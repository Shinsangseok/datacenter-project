"""Secret-file input; DEV and PROD use the same verified MySQL/TLS path."""
import json
import os
import ssl
from uuid import UUID
from pathlib import Path

# All Auth entrypoints receive PYTHONPATH=/opt/mlflow:/opt/mlflow/auth.
# ConfigMap projection symlinks are not a reliable module search root.
from db_target import environment_policy, mysql_uri, require, validate_connection

HEAD = "f1a2b3c4d5e6"


def load():
    import mlflow
    require(mlflow.__version__ == "3.16.1")
    policy = json.loads(Path(os.environ.get("AUTH_POLICY", "/etc/mlflow-auth/policy/policy.json")).read_text())
    secret = json.loads(Path(os.environ.get("AUTH_DB_SECRET", "/etc/mlflow-auth/db/connection.json")).read_text())
    # Auth has no legacy/implicit environment escape. All entrypoints use the
    # same independently approved environment policy and connection checks.
    require(bool(os.environ.get("MLFLOW_ENV")))
    approved = environment_policy(os.environ)
    require(policy["mode"] == approved["mode"])
    require(policy["expected_database"] == approved["auth_database"])
    require(policy["expected_head"] == HEAD)
    require(policy["tls_required"] is True and bool(policy["ca_file"]))
    expected_host = os.environ.get("MLFLOW_EXPECTED_AUTH_HOST")
    # A concrete legacy policy cannot silently disagree with a new approval input.
    policy_host = policy.get("expected_host", "")
    if policy_host and not policy_host.startswith("REPLACE_"):
        require(policy_host == expected_host)
    expected_user = os.environ.get("MLFLOW_EXPECTED_AUTH_USER")
    require(bool(expected_host) and bool(expected_user) and not expected_user.startswith("REPLACE_"))
    server = os.environ.get("MLFLOW_EXPECTED_SERVER_UUID", policy.get("expected_server_uuid", ""))
    require(isinstance(server, str) and str(UUID(server)) == server and UUID(server).int != 0)
    policy_server = policy.get("expected_server_uuid")
    require(not policy_server or policy_server.startswith("REPLACE_") or policy_server == server)
    policy["expected_server_uuid"] = server
    validate_connection(secret, host=expected_host, database=policy["expected_database"], username=expected_user)
    require(secret["database"] != os.environ.get("DB_NAME"))
    return policy, secret


def connect_args(policy, secret):
    require(policy["tls_required"] is True)
    context = ssl.create_default_context(cafile=policy["ca_file"])
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return dict(host=secret["host"], port=3306, user=secret["username"],
                password=secret["password"], database=secret["database"],
                connect_timeout=10, read_timeout=120, write_timeout=30, ssl=context)


def uri(policy, secret):
    require(policy["tls_required"] is True)
    # Preserve Auth's reviewed charset as well as certificate/hostname verification.
    from sqlalchemy.engine import make_url
    return make_url(mysql_uri(secret, policy["ca_file"])).update_query_dict(
        {"charset": "utf8mb4"}).render_as_string(hide_password=False)
