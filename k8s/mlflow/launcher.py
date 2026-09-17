"""Start pinned MLflow using a runtime-only URI kept out of argv and logs."""
import os
import re
import signal
import subprocess
import sys
from urllib.parse import quote, quote_plus


def redact(line, values):
    line = re.sub(r"mysql(?:\+pymysql)?://[^\s\"']+", "[REDACTED_DB_URI]", line)
    for value in sorted(set(values), key=len, reverse=True):
        if value:
            line = line.replace(value, "[REDACTED]")
    return line


def main():
    from sqlalchemy import URL, create_engine, text
    import mlflow

    assert mlflow.__version__ == "3.16.1"
    assert os.environ["DB_NAME"] == "mlflow"
    assert os.environ["DB_PORT"] == "3306"
    ca = os.environ["MLFLOW_MYSQL_SSL_CA"]
    url = URL.create(
        "mysql+pymysql", username=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"], host=os.environ["DB_HOST"],
        port=3306, database="mlflow",
        query={"ssl_verify_cert": "true", "ssl_verify_identity": "true"},
    )
    # Fail closed before MLflow can initialize an empty/wrong database.
    engine = create_engine(url, hide_parameters=True, connect_args={"ssl_ca": ca})
    with engine.connect() as conn:
        assert conn.execute(text("SELECT DATABASE()")).scalar_one() == "mlflow"
        assert conn.execute(text("SHOW SESSION STATUS LIKE 'Ssl_cipher'")).one()[1]
        assert conn.execute(text("SELECT version_num FROM mlflow.alembic_version")).scalar_one() == "b7e2c1a4d9f3"
        assert conn.execute(text("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='mlflow'")).scalar_one() == 59
    engine.dispose()
    env = os.environ.copy()
    env["MLFLOW_BACKEND_STORE_URI"] = url.render_as_string(hide_password=False)
    values = [env[k] for k in ("DB_USER", "DB_PASSWORD", "DB_HOST", "MLFLOW_BACKEND_STORE_URI")]
    values += [encode(v, safe="") for v in values[:] for encode in (quote, quote_plus)]
    command = [
        sys.executable, "-m", "mlflow", "server", "--host", "0.0.0.0",
        "--port", "5000", "--workers", "1", "--serve-artifacts",
        "--artifacts-destination", "/mlflow/artifacts",
        "--default-artifact-root", "mlflow-artifacts:/",
        "--allowed-hosts", env["MLFLOW_SERVER_ALLOWED_HOSTS"],
        "--cors-allowed-origins", "http://localhost:5000,http://127.0.0.1:5000",
    ]
    child = subprocess.Popen(command, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, start_new_session=True)

    def forward(signum, _frame):
        if child.poll() is None:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    for line in child.stdout:
        print(redact(line, values), end="", flush=True)
    return child.wait()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        # Do not format exception messages/tracebacks containing connection details.
        print("MLflow startup failed: " + type(error).__name__, file=sys.stderr)
        sys.exit(1)
