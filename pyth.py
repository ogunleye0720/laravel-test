import json
import logging
import os

import boto3
import psycopg2
from psycopg2 import sql

logging.getLogger().setLevel(logging.INFO)
log = logging.getLogger(__name__)

VALID_PRIVILEGES = {"read", "readwrite", "migrate", "app_admin"}

VALID_ACTIONS = {"create", "update", "delete"}


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


def _schema_exists(cur, schema: str) -> bool:
    """Return True when the schema exists in the current database."""
    cur.execute(
        "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
        (schema,),
    )
    return cur.fetchone() is not None


def _require_schema(cur, schema: str) -> None:
    """Fail with a readable message rather than a bare psycopg2 error."""
    if not _schema_exists(cur, schema):
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

    cur.execute(
        sql.SQL("ALTER ROLE {} WITH PASSWORD NULL").format(sql.Identifier(username))
    )
    
    cur.execute(sql.SQL("GRANT rds_iam TO {}").format(sql.Identifier(username)))


def _has_effective_role_membership(cur, username: str, role_name: str) -> bool:
    cur.execute("SELECT pg_has_role(%s, %s, 'MEMBER')", (username, role_name))
    return bool(cur.fetchone()[0])


def _revoke_global_managed_privileges(cur, username: str) -> None:
    ident = sql.Identifier(username)
    database = sql.Identifier(os.environ["DB_NAME"])
    
    if _has_effective_role_membership(cur, username, "rds_superuser"):
        log.info("revoking rds_superuser from role %s", username)
        cur.execute(sql.SQL("REVOKE rds_superuser FROM {}").format(ident))

    cur.execute(
        sql.SQL("REVOKE CONNECT, CREATE, TEMPORARY ON DATABASE {} FROM {}").format(
            database, ident
        )
    )


def _revoke_schema_managed_privileges(cur, username: str, schema: str) -> None:
    ident = sql.Identifier(username)
    sch = sql.Identifier(schema)

    log.info("resetting managed privileges for role=%s schema=%s", username, schema)

    cur.execute(sql.SQL("REVOKE USAGE, CREATE ON SCHEMA {} FROM {}").format(sch, ident))
    cur.execute(
        sql.SQL(
            "REVOKE SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} FROM {}"
        ).format(sch, ident)
    )
    cur.execute(
        sql.SQL("REVOKE USAGE ON ALL SEQUENCES IN SCHEMA {} FROM {}").format(sch, ident)
    )

    cur.execute(
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA {} "
            "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {}"
        ).format(sch, ident)
    )
    cur.execute(
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA {} REVOKE USAGE ON SEQUENCES FROM {}"
        ).format(sch, ident)
    )


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


def _apply_privileges(cur, username: str, privileges: str, schema: str) -> None:
    ident = sql.Identifier(username)
    sch = sql.Identifier(schema)
    database = sql.Identifier(os.environ["DB_NAME"])

    cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, ident))
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
            sql.SQL("GRANT ALL PRIVILEGES ON DATABASE {} TO {}").format(database, ident)
        )


def _verify_admin_state(cur, username: str, privileges: str) -> None:
    if privileges == "app_admin":
        return

    if _has_effective_role_membership(cur, username, "rds_superuser"):
        raise RuntimeError(
            f"role '{username}' still has effective rds_superuser membership "
            f"after being reconciled to '{privileges}'. REVOKE only removes "
            "grants made by this bootstrap, so the membership is inherited "
            "through another role or was granted outside Terraform. Refusing "
            "to report a successful privilege downgrade - resolve the other "
            "grant, then re-apply."
        )


def _reconcile_user(cur, user: dict, previous_user) -> None:
    username = user["username"]
    privileges = user.get("privileges", "readwrite")
    schema = user.get("schema", "public")

    _require_schema(cur, schema)
    _ensure_role(cur, username)
    _revoke_global_managed_privileges(cur, username)

    schemas_to_reset = {schema}
    if previous_user is not None:
        schemas_to_reset.add(previous_user.get("schema", "public"))

    for managed_schema in schemas_to_reset:
        if _schema_exists(cur, managed_schema):
            _revoke_schema_managed_privileges(cur, username, managed_schema)
        else:
            log.info(
                "previous managed schema %s no longer exists; "
                "skipping privilege cleanup for role %s",
                managed_schema,
                username,
            )

    _apply_privileges(cur, username, privileges, schema)

    _verify_admin_state(cur, username, privileges)


def _drop_role(cur, username: str) -> None:
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (username,))
    if cur.fetchone() is None:
        log.info("role %s does not exist; nothing to drop", username)
        return

    log.info("dropping role %s", username)
    cur.execute(
        sql.SQL("REASSIGN OWNED BY {} TO CURRENT_USER").format(sql.Identifier(username))
    )
    cur.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(username)))
    cur.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(username)))


def _username_of(user) -> str:
    """Extract a username, failing with a readable message rather than KeyError."""
    if not isinstance(user, dict) or not user.get("username"):
        raise ValueError(
            "each database user must be an object with a non-empty 'username'; "
            f"got {user!r}"
        )
    return user["username"]


def _validate(users) -> None:
    for user in users:
        username = _username_of(user)

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
    tf = event.get("tf") or {}
    action = tf.get("action") or "create"

    if action not in VALID_ACTIONS:
        raise ValueError(
            f"invalid Terraform lifecycle action '{action}'; "
            f"expected one of {sorted(VALID_ACTIONS)}"
        )

    desired = event.get("users") or []
    previous = (tf.get("prev_input") or {}).get("users") or []

    if action != "delete":
        _validate(desired)

    log.info(
        "action=%s desired=%s previous=%s",
        action,
        [u.get("username") for u in desired if isinstance(u, dict)],
        [u.get("username") for u in previous if isinstance(u, dict)],
    )

    created, dropped = [], []
    conn = _connect()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            if action == "delete":
                for user in desired or previous:
                    username = _username_of(user)
                    _drop_role(cur, username)
                    dropped.append(username)

            else:
                previous_by_username = {
                    _username_of(u): u for u in previous if isinstance(u, dict)
                }
                desired_usernames = {_username_of(u) for u in desired}
                for user in desired:
                    username = _username_of(user)
                    _reconcile_user(cur, user, previous_by_username.get(username))
                    created.append(username)
                for username in set(previous_by_username) - desired_usernames:
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
