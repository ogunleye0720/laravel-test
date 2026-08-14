"""
Creates passwordless IAM-authenticated database roles in Aurora PostgreSQL.

Invoked by Terraform (aws_lambda_invocation, lifecycle_scope = "CRUD").
Resolves the RDS-managed master secret at runtime so that no credential is
ever written to Terraform state.

Every role created here has NO PASSWORD. Authentication happens via
`GRANT rds_iam`, which makes PostgreSQL accept the SigV4 token that RDS
validates on the client's behalf.

Event shape (Terraform supplies the `tf` object under lifecycle_scope=CRUD):

    {
      "users": [ {"username": ..., "privileges": ..., "schema": ...}, ... ],
      "tf": {
        "action": "create" | "update" | "delete",
        "prev_input": { "users": [...] }        # update and delete only
      }
    }

Idempotent: safe to invoke repeatedly with the same input. The whole run is
one transaction, so a failure part-way leaves the database untouched.
"""

import json
import logging
import os

import boto3
import psycopg2
from psycopg2 import sql

logging.getLogger().setLevel(logging.INFO)
log = logging.getLogger(__name__)

# Kept in step with the `privileges` validation in modules/aurora-iam/variables.tf.
# Terraform rejects a bad value at plan time; this is the backstop for a direct
# invocation that bypasses Terraform.
VALID_PRIVILEGES = {"read", "readwrite", "migrate", "app_admin"}


def _master_credentials():
    client = boto3.client("secretsmanager")
    raw = client.get_secret_value(SecretId=os.environ["MASTER_SECRET_ARN"])
    secret = json.loads(raw["SecretString"])
    return secret["username"], secret["password"]


def _connect():
    username, password = _master_credentials()
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        dbname=os.environ["DB_NAME"],
        user=username,
        password=password,
        sslmode="require",
        connect_timeout=10,
    )


def _require_schema(cur, schema: str) -> None:
    """Fail with a readable message rather than a bare psycopg2 error."""
    cur.execute(
        "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
        (schema,),
    )
    if cur.fetchone() is None:
        raise ValueError(
            f"schema '{schema}' does not exist in database "
            f"'{os.environ['DB_NAME']}'. Create it in a migration before "
            f"granting access to it."
        )


def _ensure_role(cur, username: str) -> None:
    """Create a LOGIN role with no password and grant rds_iam."""
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (username,))
    if cur.fetchone() is None:
        log.info("creating role %s", username)
        cur.execute(sql.SQL("CREATE ROLE {} WITH LOGIN").format(sql.Identifier(username)))
    else:
        log.info("role %s already exists", username)

    # Defensive: strip any password that may have been set out of band.
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
    # Default privileges cover tables created LATER, but only those created by
    # the role running this statement (the master user). Objects created by a
    # migration running as someone else are not covered.
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


def _apply_privileges(cur, username: str, privileges: str, schema: str) -> None:
    ident = sql.Identifier(username)
    sch = sql.Identifier(schema)

    cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sch, ident))

    if privileges == "read":
        _grant_read(cur, ident, sch)

    elif privileges == "readwrite":
        _grant_readwrite(cur, ident, sch)

    elif privileges == "migrate":
        # Documented as "readwrite plus DDL", so it must actually include the
        # sequence and default-privilege grants that readwrite makes - a
        # migration that creates a table needs its sequences too.
        _grant_readwrite(cur, ident, sch)
        cur.execute(sql.SQL("GRANT CREATE ON SCHEMA {} TO {}").format(sch, ident))

    elif privileges == "app_admin":
        # Master-equivalent access delivered via IAM, so that the real master
        # user keeps password authentication and stays usable as break-glass.
        #
        # rds_superuser is the highest privilege Aurora PostgreSQL exposes;
        # there is no true SUPERUSER on managed RDS. This is enough for the
        # application team to create their own confined users later.
        cur.execute(sql.SQL("GRANT rds_superuser TO {}").format(ident))
        cur.execute(sql.SQL("GRANT CREATE ON SCHEMA {} TO {}").format(sch, ident))
        cur.execute(
            sql.SQL("GRANT ALL PRIVILEGES ON DATABASE {} TO {}").format(
                sql.Identifier(os.environ["DB_NAME"]), ident
            )
        )


def _drop_role(cur, username: str) -> None:
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (username,))
    if cur.fetchone() is None:
        return

    log.info("dropping role %s", username)
    # REASSIGN/DROP OWNED only reach objects in the CURRENT database. A role
    # that owns objects in another database on this cluster will still block
    # DROP ROLE - deliberately, since silently discarding them would be worse.
    cur.execute(
        sql.SQL("REASSIGN OWNED BY {} TO CURRENT_USER").format(sql.Identifier(username))
    )
    cur.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(username)))
    cur.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(username)))


def _validate(users) -> None:
    for user in users:
        privileges = user.get("privileges", "readwrite")
        if privileges not in VALID_PRIVILEGES:
            raise ValueError(
                f"invalid privileges '{privileges}' for {user['username']}; "
                f"expected one of {sorted(VALID_PRIVILEGES)}"
            )


def lambda_handler(event, _context):
    tf = event.get("tf", {})
    action = tf.get("action", "create")

    desired = event.get("users", [])
    previous = tf.get("prev_input", {}).get("users", [])

    _validate(desired)

    log.info(
        "action=%s desired=%s previous=%s",
        action,
        [u["username"] for u in desired],
        [u["username"] for u in previous],
    )

    created, dropped = [], []
    conn = _connect()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            if action == "delete":
                for user in desired:
                    _drop_role(cur, user["username"])
                    dropped.append(user["username"])
            else:
                for user in desired:
                    username = user["username"]
                    schema = user.get("schema", "public")
                    _require_schema(cur, schema)
                    _ensure_role(cur, username)
                    _apply_privileges(
                        cur, username, user.get("privileges", "readwrite"), schema
                    )
                    created.append(username)

                # Users removed from db_users between applies. Without this
                # they would linger with rds_iam intact - still reachable by
                # any role holding rds-db:connect for that username.
                for username in {u["username"] for u in previous} - {
                    u["username"] for u in desired
                }:
                    _drop_role(cur, username)
                    dropped.append(username)

        conn.commit()
    except Exception:
        conn.rollback()
        log.exception("bootstrap failed; rolled back")
        raise
    finally:
        conn.close()

    return {"status": "ok", "action": action, "created": created, "dropped": dropped}
