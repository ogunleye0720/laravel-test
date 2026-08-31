import logging
import os

import psycopg2
from psycopg2 import sql

logging.getLogger().setLevel(logging.INFO)
log = logging.getLogger(__name__)

# Fixed, not configurable. Terraform has no use for either - nothing in the
# IAM policy or the module wiring reads them - so they stay here.
DB_SCHEMA = "public"

# Applied inside the transaction. Without these a blocked DDL statement waits
# on a lock until the Lambda is killed, leaving no useful error.
LOCK_TIMEOUT = "10s"
STATEMENT_TIMEOUT = "60s"


def _parse_database_url(uri: str, source: str) -> dict:
    """
    Split a postgresql://user:password@host:port/dbname URI.

    Deliberately NOT urllib.parse.urlsplit. The URI is not percent-encoded, and
    urlsplit treats '?' and '#' in an unencoded password as query and fragment
    delimiters - returning password=None. Both characters are legal in an RDS
    master password and both are in random_password's default set.

    The splits below are safe because RDS forbids '/', '"', '@' and space in a
    master password, so the first '/' after the scheme and the '@' before the
    host are unambiguous. Splitting userinfo on the FIRST ':' keeps a password
    containing ':' intact - which is where the naive
    `user_pass.split(':')[1]` form silently truncates it.
    """
    if "://" not in uri:
        raise ValueError(
            f"{source} is not a database connection URI. "
            "Expected postgresql://user:password@host:port/dbname"
        )

    _, _, rest = uri.partition("://")
    netloc, _, dbname = rest.partition("/")
    userinfo, separator, hostport = netloc.rpartition("@")

    if not separator or not userinfo or not dbname:
        raise ValueError(
            f"{source} is missing credentials, host or "
            "database name. Expected postgresql://user:password@host:port/dbname"
        )

    user, _, password = userinfo.partition(":")
    host, _, port = hostport.rpartition(":")
    if not host:
        host, port = hostport, "5432"

    log.info(
        "master URI parsed: host=%s port=%s dbname=%s user=%s", host, port, dbname, user
    )
    return {"host": host, "port": port, "dbname": dbname, "user": user, "password": password}


def _master_connection_params() -> dict:
    """
    Parse the master connection URI Terraform injected.

    No SSM call and no boto3: the URI arrives in the environment already. That
    removes the one network dependency this function had before it could reach
    the database, which in practice was taking between 4 and 13 seconds and
    retrying on the way.

    The SSM parameter still exists, but for humans - break-glass and debugging.
    """
    log.info("using master connection URI from the DB_URL environment variable")
    return _parse_database_url(os.environ["DB_URL"], "DB_URL")


def _connect(params: dict):
    """Connect as the master user."""
    log.info("connecting to %s:%s as %s", params["host"], params["port"], params["user"])
    return psycopg2.connect(
        host=params["host"],
        port=params["port"],
        dbname=params["dbname"],
        user=params["user"],
        password=params["password"],
        sslmode="require",
        connect_timeout=10,
    )


def _require_schema(cur, schema: str, dbname: str) -> None:
    """Fail with a readable message rather than a bare psycopg2 error."""
    cur.execute(
        "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
        (schema,),
    )
    if cur.fetchone() is None:
        raise ValueError(
            f"schema '{schema}' does not exist in database '{dbname}'. "
            "Create it in a migration before granting access to it."
        )


def _ensure_role(cur, username: str) -> None:
    """Create a LOGIN role with no password and grant rds_iam."""
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (username,))
    if cur.fetchone() is None:
        log.info("creating role %s", username)
        cur.execute(sql.SQL("CREATE ROLE {} WITH LOGIN").format(sql.Identifier(username)))
    else:
        log.info("role %s already exists", username)

    # Defensive: strip any password that may have been set out of band. This
    # role is deliberately IAM-only.
    cur.execute(
        sql.SQL("ALTER ROLE {} WITH PASSWORD NULL").format(sql.Identifier(username))
    )
    cur.execute(sql.SQL("GRANT rds_iam TO {}").format(sql.Identifier(username)))


def _grant_app_admin(cur, username: str, schema: str, dbname: str) -> None:
    """
    Grant the app_admin privilege set - the only level this module issues.

    Two halves, and both are needed:

      * rds_superuser and database-level rights, which is what lets application
        teams create their own confined users beneath this one. That capability
        is the reason rds_superuser is here at all.

      * ordinary table and sequence grants. rds_superuser is NOT a true
        PostgreSQL superuser and does not bypass table-level permission checks,
        so without these the role gets permission denied on any object it does
        not own. Invisible while it creates everything itself; it appears the
        first time a migration runs as another role.
    """
    ident = sql.Identifier(username)
    sch = sql.Identifier(schema)
    db = sql.Identifier(dbname)

    # --- administrative -----------------------------------------------------
    cur.execute(sql.SQL("GRANT rds_superuser TO {}").format(ident))
    cur.execute(sql.SQL("GRANT ALL PRIVILEGES ON DATABASE {} TO {}").format(db, ident))
    cur.execute(sql.SQL("GRANT ALL PRIVILEGES ON SCHEMA {} TO {}").format(sch, ident))

    # --- objects that already exist ----------------------------------------
    cur.execute(
        sql.SQL(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}"
        ).format(sch, ident)
    )
    cur.execute(
        sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}").format(sch, ident)
    )

    # --- objects created later ---------------------------------------------
    # Covers only objects created by the role running these statements (the
    # master user). Anything this role creates itself, it owns outright and
    # needs no grant for.
    cur.execute(
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA {} "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
        ).format(sch, ident)
    )
    cur.execute(
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT USAGE, SELECT ON SEQUENCES TO {}"
        ).format(sch, ident)
    )


def lambda_handler(event, _context):
    username = (event or {}).get("username")
    if not username or not isinstance(username, str):
        raise ValueError("event must supply a non-empty string 'username'")

    # `tf` is only present if lifecycle_scope is left at CRUD. This seed does
    # not manage removal, so a delete is a no-op - and returning here, BEFORE
    # touching the database, is what stops a destroy from hanging on an
    # unreachable endpoint just to do nothing.
    action = (event.get("tf") or {}).get("action") or "create"
    if action == "delete":
        log.info("action=delete: this bootstrap does not drop roles; nothing to do")
        return {"status": "skipped", "action": action, "username": username}

    log.info("action=%s username=%s schema=%s", action, username, DB_SCHEMA)

    params = _master_connection_params()
    dbname = params["dbname"]

    conn = _connect(params)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
            cur.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")

            _require_schema(cur, DB_SCHEMA, dbname)
            _ensure_role(cur, username)
            _grant_app_admin(cur, username, DB_SCHEMA, dbname)

        conn.commit()
        log.info("bootstrap committed: %s", username)
    except Exception:
        conn.rollback()
        log.exception("bootstrap failed; rolled back")
        raise
    finally:
        conn.close()

    # Returned into Terraform state. Usernames only - the role has no password,
    # so there is nothing sensitive here.
    return {
        "status": "ok",
        "action": action,
        "username": username,
        "schema": DB_SCHEMA,
        "privileges": "app_admin",
    }
