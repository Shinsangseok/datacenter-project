"""Explicit one-shot DEV schema initialization, never imported by FastAPI."""
import argparse
import json
import os
from pathlib import Path
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool
from backend.db_config import database_url
from backend.schema import CREATE_TABLE_SQL, validate_measurements


def require(condition):
    if not condition:
        raise RuntimeError('DEV schema initialization rejected')


def initialize(env, policy):
    # Approval inputs come from a separate private policy, not the connection env.
    require(env.get('MLFLOW_ENV') == 'dev')
    require(policy.get('expected_database') == env.get('DB_NAME') == 'datacenter_app_dev')
    require(policy.get('tls_required') is True and bool(env.get('DB_SSL_CA')))
    require(bool(policy.get('expected_host')) and policy['expected_host'] == env.get('DB_HOST'))
    require(bool(policy.get('expected_user')) and policy['expected_user'] == env.get('DB_USER'))
    require(not policy['expected_host'].startswith('REPLACE_'))
    approved_env = dict(env, DB_EXPECTED_HOST=policy['expected_host'],
                        DB_EXPECTED_USER=policy['expected_user'],
                        DB_EXPECTED_NAME=policy['expected_database'])
    # No pool reuse, pre-ping, reconnect or automatic retry for this one-shot tool.
    engine = create_engine(database_url(approved_env), hide_parameters=True, poolclass=NullPool,
                           connect_args={'connect_timeout': 10, 'read_timeout': 30, 'write_timeout': 30})
    try:
        with engine.begin() as conn:
            require(conn.execute(text('SELECT DATABASE()')).scalar_one() == 'datacenter_app_dev')
            require(bool(conn.execute(text("SHOW SESSION STATUS LIKE 'Ssl_cipher'")).one()[1]))
            objects = conn.execute(text(
                'SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES '
                'WHERE TABLE_SCHEMA=DATABASE() ORDER BY TABLE_NAME')).all()
            require(objects in [[], [('measurements', 'BASE TABLE')]])
            for table, column in [('ROUTINES', 'ROUTINE_SCHEMA'), ('TRIGGERS', 'TRIGGER_SCHEMA'), ('EVENTS', 'EVENT_SCHEMA')]:
                require(conn.execute(text('SELECT COUNT(*) FROM information_schema.' + table +
                                          ' WHERE ' + column + '=DATABASE()')).scalar_one() == 0)
            if not objects:
                conn.execute(text(CREATE_TABLE_SQL))
            validate_measurements(conn)
            return 'created' if not objects else 'verified_existing'
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', required=True, help='Private approved target JSON path; no credential arguments')
    args = parser.parse_args()
    try:
        policy = json.loads(Path(args.policy).read_text())
        result = initialize(os.environ, policy)
        print('DEV measurements schema: ' + result)
        return 0
    except Exception as error:
        print('DEV schema initialization rejected: ' + type(error).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
