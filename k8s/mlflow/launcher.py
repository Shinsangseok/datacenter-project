"""Start pinned MLflow using a runtime-only URI kept out of argv and logs."""
import os
import signal
import subprocess
import sys
from db_target import mysql_uri, redact, require, sensitive_values, tracking_connection, verify_tracking


def main():
    from sqlalchemy import create_engine
    import mlflow

    require(mlflow.__version__ == "3.16.1")
    connection = tracking_connection(os.environ)
    url = mysql_uri(connection, os.environ["MLFLOW_MYSQL_SSL_CA"])
    # No migration/initialization here: verify the selected schema before startup.
    verify_tracking(create_engine(url, hide_parameters=True), connection["database"])
    env = os.environ.copy()
    env["MLFLOW_BACKEND_STORE_URI"] = url
    values = sensitive_values(env)
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
