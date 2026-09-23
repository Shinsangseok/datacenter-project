"""Pinned Auth server: identical RDS/TLS startup path in DEV and PROD."""
import configparser
import os
from pathlib import Path
import signal
import subprocess
import sys

from sqlalchemy import create_engine, text
from auth_config import HEAD, load, require, uri
from db_target import mysql_uri, redact, sensitive_values, tracking_connection, verify_tracking


def write_auth_config(url):
    os.umask(0o077)
    path = Path('/run/mlflow-auth/auth.ini')
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = configparser.ConfigParser(interpolation=None)
    cfg['mlflow'] = {'default_permission': 'NO_PERMISSIONS', 'database_uri': url.replace('%', '%%')}
    with path.open('w') as handle:
        cfg.write(handle)
    path.chmod(0o600)
    return str(path)


def prepare():
    policy, secret = load()
    # Validate both targets before any connection or output file is created.
    track = tracking_connection(os.environ)
    require(track['host'] == secret['host'])  # The approved architecture shares one RDS.
    require(track['database'] != secret['database'] and track['username'] != secret['username'])
    require(bool(os.environ.get('MLFLOW_FLASK_SERVER_SECRET_KEY')))
    require(not os.environ.get('MLFLOW_AUTH_ADMIN_PASSWORD'))
    url = uri(policy, secret)
    engine = create_engine(url, hide_parameters=True)
    try:
        with engine.connect() as conn:
            require(conn.execute(text('SELECT DATABASE()')).scalar_one() == secret['database'])
            require(bool(conn.execute(text("SHOW SESSION STATUS LIKE 'Ssl_cipher'")).one()[1]))
            require(conn.execute(text('SELECT version_num FROM alembic_version_auth')).scalars().all() == [HEAD])
            require(conn.execute(text('SELECT COUNT(*) FROM users WHERE is_admin=1')).scalar_one() > 0)
    finally:
        engine.dispose()
    backend = mysql_uri(track, os.environ['MLFLOW_MYSQL_SSL_CA'])
    verify_tracking(create_engine(backend, hide_parameters=True), track['database'])
    env = os.environ.copy()
    env['MLFLOW_AUTH_CONFIG_PATH'] = write_auth_config(url)
    env['MLFLOW_BACKEND_STORE_URI'] = backend
    values = sensitive_values(env, [url, secret['host'], secret['username'], secret['password']])
    cmd = [sys.executable, '-B', '-m', 'mlflow', 'server', '--app-name', 'basic-auth',
           '--host', '0.0.0.0', '--port', '5000', '--workers', '1', '--serve-artifacts',
           '--artifacts-destination', '/mlflow/artifacts', '--default-artifact-root',
           'mlflow-artifacts:/', '--allowed-hosts', env['MLFLOW_SERVER_ALLOWED_HOSTS']]
    return cmd, env, values


def main():
    cmd, env, values = prepare()
    child = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, start_new_session=True)

    def forward(signum, _frame):
        if child.poll() is None:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    for line in child.stdout:
        print(redact(line, values), end='', flush=True)
    return child.wait()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print('Auth startup rejected: ' + type(error).__name__)
        sys.exit(1)
