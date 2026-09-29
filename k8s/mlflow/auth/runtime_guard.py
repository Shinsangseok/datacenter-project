"""Shared Auth startup invariants, independent of legitimate client/role row counts."""
import json
import os
from pathlib import Path
import re

from sqlalchemy import create_engine, event
from sqlalchemy.pool import NullPool

from auth_config import HEAD, require, uri
from schema_fingerprint import FORMAT, load_context, schema_snapshot


def validate_schema(expected, actual, revision):
    require(expected['format'] == FORMAT and expected['revision'] == [HEAD] == revision)
    require(set(expected['tables']) == set(actual) and len(actual) == 12)
    for name, digest in expected['tables'].items():
        require(re.fullmatch(FORMAT + r':[a-f0-9]{64}', digest) is not None)
        require(actual[name] == digest)


def validate_users(rows, admin_username):
    """Retain the sole configured bootstrap administrator; allow ordinary clients.

    The schema hash also verifies the DB's collation-aware unique username index.
    No live data is used to generate or approve an expected schema fingerprint.
    """
    require(bool(admin_username) and bool(rows))
    ids, names, admins = set(), set(), []
    for user_id, username, password_hash, is_admin in rows:
        require(isinstance(user_id, int) and user_id > 0 and user_id not in ids)
        require(isinstance(username, str) and bool(username.strip()))
        require(username.casefold() not in names)
        require(isinstance(password_hash, str) and bool(password_hash.strip()))
        require(is_admin in (0, 1))
        ids.add(user_id)
        names.add(username.casefold())
        if is_admin:
            admins.append(username)
    require(admins == [admin_username])


def verify(connection, expected, admin_username):
    names = connection.exec_driver_sql(
        'SELECT TABLE_NAME,TABLE_TYPE FROM information_schema.TABLES '
        'WHERE TABLE_SCHEMA=DATABASE() ORDER BY TABLE_NAME').all()
    require(all(kind == 'BASE TABLE' for _, kind in names))
    require({name for name, _ in names} == set(expected['tables']))
    for source, field in [('ROUTINES', 'ROUTINE_SCHEMA'), ('TRIGGERS', 'TRIGGER_SCHEMA'),
                          ('EVENTS', 'EVENT_SCHEMA')]:
        require(connection.exec_driver_sql('SELECT COUNT(*) FROM information_schema.' + source +
                                          ' WHERE ' + field + '=DATABASE()').scalar_one() == 0)
    context = load_context(connection)
    actual = {}
    for name, _ in names:
        require(re.fullmatch('[a-z_]+', name) is not None)
        ddl = connection.exec_driver_sql('SHOW CREATE TABLE `' + name + '`').one()[1]
        actual[name] = schema_snapshot(connection, name, ddl, context)['schema']
    revision = connection.exec_driver_sql('SELECT version_num FROM alembic_version_auth').scalars().all()
    validate_schema(expected, actual, revision)
    users = connection.exec_driver_sql('SELECT id,username,password_hash,is_admin FROM users').all()
    validate_users(users, admin_username)


def verify_runtime(policy, secret):
    """Mandatory server preflight, also on direct auth_server.py startup."""
    require(bool(policy.get('expected_server_uuid')))
    require(not os.environ.get('MLFLOW_AUTH_ADMIN_PASSWORD'))
    expected = json.loads(Path(__file__).with_name('expected-schema-v2.json').read_text())
    engine = create_engine(uri(policy, secret), poolclass=NullPool, hide_parameters=True)
    @event.listens_for(engine, 'before_cursor_execute')
    def readonly(conn, cursor, statement, params, context, many):
        require(statement.strip().split()[0].upper() in ('SELECT', 'SHOW'))
    try:
        with engine.connect() as connection:
            db, server, account, role = connection.exec_driver_sql(
                'SELECT DATABASE(),@@server_uuid,CURRENT_USER(),CURRENT_ROLE()').one()
            require(db == secret['database'] and server == policy['expected_server_uuid'])
            require(account.split('@')[0] == secret['username'] and role == 'NONE')
            require(bool(connection.exec_driver_sql("SHOW SESSION STATUS LIKE 'Ssl_cipher'").one()[1]))
            verify(connection, expected, os.environ['MLFLOW_AUTH_ADMIN_USERNAME'])
    finally:
        engine.dispose()


def main():
    import auth_server
    return auth_server.main()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print('Auth startup rejected: ' + type(error).__name__, flush=True)
        raise SystemExit(1)
