"""Environment-selected MySQL targets with independent, fail-closed expectations."""
import json
from pathlib import Path
import re
from urllib.parse import quote, quote_plus

TRACKING_HEAD = "b7e2c1a4d9f3"
TRACKING_TABLES = 59
SYSTEM_DATABASES = {"mysql", "sys", "information_schema", "performance_schema"}


def require(condition):
    if not condition:
        raise RuntimeError("configuration or safety gate rejected")


def environment(env):
    mode = env.get("MLFLOW_ENV", "prod")
    require(mode in {"prod", "dev"})
    return mode


def environment_policy(env):
    """Reviewed non-secret targets shared by every environment and entrypoint."""
    policies = json.loads(Path(__file__).with_name("environment-targets.json").read_text())
    require(set(policies) == {"dev", "prod"})
    databases = [p[role] for p in policies.values()
                 for role in ("tracking_database", "auth_database")]
    require(len(set(databases)) == len(databases))
    require(all(isinstance(db, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,39}", db)
                and db not in SYSTEM_DATABASES for db in databases))
    return policies[environment(env)]


def validate_connection(connection, *, host, database, username=None):
    require(isinstance(host, str) and bool(host) and not host.startswith("REPLACE_"))
    require(isinstance(database, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,39}", database))
    require(database not in SYSTEM_DATABASES)
    require(connection["host"] == host and connection["database"] == database)
    require(int(connection.get("port", 3306)) == 3306)
    require(bool(connection["username"] and connection["password"]))
    if username is not None:
        require(bool(username) and connection["username"] == username)


def tracking_connection(env):
    policy = environment_policy(env)
    connection = dict(host=env["DB_HOST"], port=env["DB_PORT"], database=env["DB_NAME"],
                      username=env["DB_USER"], password=env["DB_PASSWORD"])
    # Existing PROD deployments have no MLFLOW_ENV/expected-host/user settings.
    # Keep their original DB-name gate; never use this compatibility mode for DEV.
    legacy = "MLFLOW_ENV" not in env
    expected_db = env.get("MLFLOW_EXPECTED_DB_NAME", "mlflow" if legacy else "")
    expected_host = env.get("MLFLOW_EXPECTED_DB_HOST", connection["host"] if legacy else "")
    expected_user = env.get("MLFLOW_EXPECTED_DB_USER", connection["username"] if legacy else "")
    if legacy:
        require(expected_db == "mlflow")
    require(expected_db == policy["tracking_database"])
    validate_connection(connection, host=expected_host, database=expected_db, username=expected_user)
    require(bool(env["MLFLOW_MYSQL_SSL_CA"]))
    return connection


def mysql_uri(connection, ca_file):
    from sqlalchemy.engine import URL
    require(bool(ca_file))
    return URL.create("mysql+pymysql", username=connection["username"],
                      password=connection["password"], host=connection["host"],
                      port=3306, database=connection["database"],
                      # SQLAlchemy folds these into PyMySQL's ssl dict. Do not also pass
                      # ssl_verify_*: PyMySQL would replace that dict and lose its CA.
                      # A CA plus check_hostname requires CERT_REQUIRED in PyMySQL.
                      query={"ssl_ca": ca_file, "ssl_check_hostname": "true"}).render_as_string(hide_password=False)


def verify_tracking(engine, database):
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            require(conn.execute(text("SELECT DATABASE()")).scalar_one() == database)
            require(bool(conn.execute(text("SHOW SESSION STATUS LIKE 'Ssl_cipher'")).one()[1]))
            require(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == TRACKING_HEAD)
            require(conn.execute(text("SELECT COUNT(*) FROM information_schema.TABLES "
                                      "WHERE TABLE_SCHEMA=:database"),
                                 {"database": database}).scalar_one() == TRACKING_TABLES)
    finally:
        engine.dispose()


def sensitive_values(env, extra=()):
    values = list(extra) + [v for k, v in env.items() if v and (
        any(part in k for part in ("PASSWORD", "SECRET", "TOKEN", "USERNAME", "ACCESS_KEY"))
        or k in {"DB_HOST", "DB_USER", "DB_NAME", "MLFLOW_BACKEND_STORE_URI",
                 "MLFLOW_EXPECTED_DB_HOST", "MLFLOW_EXPECTED_DB_USER", "MLFLOW_EXPECTED_AUTH_HOST",
                 "MLFLOW_EXPECTED_AUTH_USER"})]
    return values + [encode(v, safe="") for v in values for encode in (quote, quote_plus)]


def redact(line, values):
    line = re.sub(r"mysql(?:\+pymysql)?://[^\s\"']+", "[REDACTED_DB_URI]", line)
    line = re.sub(r"(?i)([?&](?:username|password)=)[^&\s\"']+", r"\1[REDACTED]", line)
    for value in sorted(set(values), key=len, reverse=True):
        if value:
            line = line.replace(value, "[REDACTED]")
    return line
