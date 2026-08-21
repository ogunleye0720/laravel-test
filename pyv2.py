import logging
import os

import boto3
import psycopg2
from botocore.config import Config
from psycopg2 import sql

logging.getLogger().setLevel(logging.INFO)
log = logging.getLogger(__name__)

VALID_PRIVILEGES = {"read", "readwrite", "migrate", "app_admin"}

_BOTO_CONFIG = Config(
    connect_timeout=5,
    read_timeout=5,
    retries={"max_attempts": 3, "mode": "standard"},
)

LOCK_TIMEOUT = "10s"
STATEMENT_TIMEOUT = "60s"


def _parse_database_url(uri: str, parameter: str) -> dict:

    if "://" not in uri:
        raise ValueError(
            f"SSM parameter '{parameter}' is not a database connection URI. "
            "Expected postgresql://user:password@host:port/dbname"
        )

    _, _, rest = uri.partition("://")
    netloc, _, dbname = rest.partition("/")
    userinfo, separator, hostport = netloc.rpartition("@")

    if not separator or not userinfo or not dbname:
        raise ValueError(
            f"SSM parameter '{parameter}' is missing credentials, host or "
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
    """Read the master connection URI from SSM and parse it."""
    parameter = os.environ["DB_URL_SSM_PARAM_NAME"]
    log.info("retrieving DB connection string from SSM parameter %s", parameter)

    ssm = boto3.client("ssm", config=_BOTO_CONFIG)

    uri = ssm.get_parameter(Name=parameter, WithDecryption=True)["Parameter"]["Value"]

    return _parse_database_url(uri, parameter)


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

    cur.execute(
        sql.SQL("ALTER ROLE {} WITH PASSWORD NULL").format(sql.Identifier(username))
    )
    cur.execute(sql.SQL("GRANT rds_iam TO {}").format(sql.Identifier(username)))


def _grant_read(cur, ident, sch) -> None:
    cur.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(sch, ident))
    cur.execute(
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT ON TABLES TO {}"
        ).format(sch, ident)
    )


def _grant_readwrite(cur, ident, sch) -> None:
    cur.execute(
        sql.SQL(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}"
        ).format(sch, ident)
    )
    cur.execute(
        sql.SQL("GRANT USAGE ON ALL SEQUENCES IN SCHEMA {} TO {}").format(sch, ident)
    )

    cur.execute(
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA {} "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
        ).format(sch, ident)
    )
    cur.execute(
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT USAGE ON SEQUENCES TO {}"
        ).format(sch, ident)
    )


def _apply_privileges(cur, username: str, privileges: str, schema: str, dbname: str) -> None:
    ident = sql.Identifier(username)
    sch = sql.Identifier(schema)

    cur.execute(
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(dbname), ident)
    )
    cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sch, ident))

    if privileges == "read":
        _grant_read(cur, ident, sch)

    elif privileges == "readwrite":
        _grant_readwrite(cur, ident, sch)

    elif privileges == "migrate":

        _grant_readwrite(cur, ident, sch)
        cur.execute(sql.SQL("GRANT CREATE ON SCHEMA {} TO {}").format(sch, ident))

    elif privileges == "app_admin":

        cur.execute(sql.SQL("GRANT rds_superuser TO {}").format(ident))
        cur.execute(sql.SQL("GRANT CREATE ON SCHEMA {} TO {}").format(sch, ident))
        cur.execute(
            sql.SQL("GRANT ALL PRIVILEGES ON DATABASE {} TO {}").format(
                sql.Identifier(dbname), ident
            )
        )


def _validate(users) -> None:
    for user in users:
        if not isinstance(user, dict):
            raise ValueError("each database user must be an object")

        username = user.get("username")
        if not username or not isinstance(username, str):
            raise ValueError("each database user needs a non-empty string 'username'")

        privileges = user.get("privileges", "readwrite")
        if privileges not in VALID_PRIVILEGES:
            raise ValueError(
                f"invalid privileges '{privileges}' for {username}; "
                f"expected one of {sorted(VALID_PRIVILEGES)}"
            )

        schema = user.get("schema", "public")
        if not schema or not isinstance(schema, str):
            raise ValueError(
                f"invalid schema for {username}; 'schema' must be a non-empty string"
            )


def lambda_handler(event, _context):
    users = event.get("users") or []
  
    action = (event.get("tf") or {}).get("action") or "create"
    if action == "delete":
        log.info("action=delete: this bootstrap does not drop roles; nothing to do")
        return {"status": "skipped", "action": action, "created": []}

    if not users:
        log.info("no users requested; nothing to do")
        return {"status": "ok", "action": action, "created": []}

    _validate(users)
    log.info("action=%s users=%s", action, [u["username"] for u in users])

    params = _master_connection_params()
    dbname = params["dbname"]

    created = []
    conn = _connect(params)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
            cur.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")

            for user in users:
                username = user["username"]
                schema = user.get("schema", "public")
                privileges = user.get("privileges", "readwrite")

                _require_schema(cur, schema, dbname)
                _ensure_role(cur, username)
                _apply_privileges(cur, username, privileges, schema, dbname)
                created.append(username)

        conn.commit()
        log.info("bootstrap committed: %s", created)
    except Exception:
        conn.rollback()
        log.exception("bootstrap failed; rolled back")
        raise
    finally:
        conn.close()

    return {"status": "ok", "action": action, "created": created}
