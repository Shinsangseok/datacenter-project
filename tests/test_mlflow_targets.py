"""Offline DEV/PROD target, TLS, startup and redaction regression tests."""
import io
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote, quote_plus

from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
CODE = ROOT / 'k8s/mlflow'
# These are also standalone scripts in the deployment bundle.
sys.path.insert(0, str(CODE))
sys.path.insert(0, str(CODE / 'auth'))
import db_target as target
import launcher
import auth_config
import auth_server

# Synthetic values only. No AWS, Kubernetes or real database connection is used.
BASE = {'DB_HOST': 'rds.example.invalid', 'DB_PORT': '3306', 'DB_NAME': 'mlflow',
        'DB_USER': 'tracking_fixture', 'DB_PASSWORD': 'synthetic/+@ password',
        'MLFLOW_MYSQL_SSL_CA': '/fixture/ca.pem',
        'MLFLOW_SERVER_ALLOWED_HOSTS': 'localhost:5000'}


def dev_env():
    return {**BASE, 'MLFLOW_ENV': 'dev', 'DB_NAME': 'mlflow_tracking_dev',
            'MLFLOW_EXPECTED_DB_HOST': BASE['DB_HOST'],
            'MLFLOW_EXPECTED_DB_NAME': 'mlflow_tracking_dev',
            'MLFLOW_EXPECTED_DB_USER': BASE['DB_USER']}


class Result:
    def __init__(self, value): self.value = value
    def scalar_one(self): return self.value
    def one(self): return self.value
    def scalars(self): return self
    def all(self): return self.value


class Engine:
    def __init__(self, database, head=target.TRACKING_HEAD, count=59, tls=True, auth=False, admin=1):
        self.database, self.head, self.count = database, head, count
        self.tls, self.auth, self.admin = tls, auth, admin
        self.queries = []
        self.disposed = False
    def connect(self): return self
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, query, params=None):
        query = str(query); self.queries.append((query, params))
        if query == 'SELECT DATABASE()': return Result(self.database)
        if 'Ssl_cipher' in query: return Result(('Ssl_cipher', 'TLS-fixture' if self.tls else ''))
        if 'alembic_version_auth' in query: return Result([self.head])
        if 'alembic_version' in query: return Result(self.head)
        if 'information_schema.TABLES' in query:
            assert params == {'database': self.database}, 'wrong schema bound to count query'
            return Result(self.count)
        if 'users WHERE is_admin' in query: return Result(self.admin)
        raise AssertionError('Unexpected SQL in read-only preflight')
    def dispose(self): self.disposed = True


class Offline(unittest.TestCase):
    def setUp(self):
        for context in [patch.dict(os.environ, BASE, clear=True),
                        patch.dict(sys.modules, {'mlflow': SimpleNamespace(__version__='3.16.1')}),
                        patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden'))]:
            context.start(); self.addCleanup(context.stop)


class TargetTests(Offline):
    def test_legacy_production_config_keeps_target(self):
        config = target.tracking_connection(BASE)
        self.assertEqual(config['database'], BASE['DB_NAME'])
        url = make_url(target.mysql_uri(config, BASE['MLFLOW_MYSQL_SSL_CA']))
        self.assertEqual(url.drivername, 'mysql+pymysql')
        self.assertEqual((url.host, url.username, url.password, url.database),
                         (BASE['DB_HOST'], BASE['DB_USER'], BASE['DB_PASSWORD'], BASE['DB_NAME']))
        self.assertEqual(url.query, {'ssl_ca': BASE['MLFLOW_MYSQL_SSL_CA'],
                                   'ssl_check_hostname': 'true'})

    def test_mysql_driver_receives_ca_and_hostname_verification(self):
        from sqlalchemy.dialects.mysql.pymysql import MySQLDialect_pymysql
        config = target.tracking_connection(dev_env())
        url = make_url(target.mysql_uri(config, BASE['MLFLOW_MYSQL_SSL_CA']))
        args, kwargs = MySQLDialect_pymysql().create_connect_args(url)
        self.assertEqual(kwargs['database'], 'mlflow_tracking_dev')
        self.assertEqual(kwargs['ssl']['ca'], BASE['MLFLOW_MYSQL_SSL_CA'])
        self.assertTrue(kwargs['ssl']['check_hostname'])
        import pymysql.connections
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with patch.object(pymysql.connections.ssl, 'create_default_context', return_value=context) as create:
            connection = pymysql.connections.Connection(defer_connect=True, **kwargs)
        create.assert_called_once_with(cafile=BASE['MLFLOW_MYSQL_SSL_CA'], capath=None)
        self.assertTrue(connection.ctx.check_hostname)
        self.assertEqual(connection.ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(connection._ssl_required)

    def test_explicit_prod_and_dev_target_configuration(self):
        prod = {**dev_env(), 'MLFLOW_ENV': 'prod', 'DB_NAME': 'mlflow', 'MLFLOW_EXPECTED_DB_NAME': 'mlflow'}
        for env in [dev_env(), prod]:
            with self.subTest(mode=env['MLFLOW_ENV']):
                config = target.tracking_connection(env)
                self.assertEqual(make_url(target.mysql_uri(config, env['MLFLOW_MYSQL_SSL_CA'])).database, env['DB_NAME'])

    def test_wrong_host_name_user_port_rejected(self):
        for key, value in [('DB_HOST', 'wrong.example.invalid'), ('DB_NAME', 'mlflow'),
                           ('DB_USER', 'wrong_user'), ('DB_PORT', '3307')]:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                target.tracking_connection({**dev_env(), key: value})

    def test_expected_target_settings_are_mandatory_for_new_environments(self):
        for mode in ['dev', 'prod']:
            env = {**dev_env(), 'MLFLOW_ENV': mode}
            if mode == 'prod': env.update(DB_NAME='mlflow', MLFLOW_EXPECTED_DB_NAME='mlflow')
            for key in ['MLFLOW_EXPECTED_DB_HOST', 'MLFLOW_EXPECTED_DB_NAME', 'MLFLOW_EXPECTED_DB_USER']:
                incomplete = dict(env); incomplete.pop(key)
                with self.subTest(mode=mode, key=key), self.assertRaises(RuntimeError):
                    target.tracking_connection(incomplete)

    def test_missing_connection_and_ca_fail_closed(self):
        for key in BASE:
            if key == 'MLFLOW_SERVER_ALLOWED_HOSTS': continue
            incomplete = dict(BASE); incomplete.pop(key)
            with self.subTest(key=key), self.assertRaises((KeyError, RuntimeError)):
                target.tracking_connection(incomplete)
        with self.assertRaises(RuntimeError):
            target.tracking_connection({**BASE, 'MLFLOW_MYSQL_SSL_CA': ''})

    def test_cross_environment_and_implicit_dev_rejected(self):
        cases = [{**dev_env(), 'MLFLOW_ENV': 'prod'},
                 {**BASE, 'DB_NAME': 'mlflow_tracking_dev'},
                 {**dev_env(), 'DB_NAME': 'mlflow', 'MLFLOW_EXPECTED_DB_NAME': 'mlflow'},
                 {**dev_env(), 'MLFLOW_ENV': 'isolated-test'}]
        for env in cases:
            with self.subTest(env=env.get('MLFLOW_ENV')), self.assertRaises(RuntimeError):
                target.tracking_connection(env)

    def test_tracking_connected_database_tls_head_and_count_gates(self):
        for db in ['mlflow', 'mlflow_tracking_dev']:
            engine = Engine(db); target.verify_tracking(engine, db)
            self.assertTrue(engine.disposed)
            self.assertTrue(all(q.startswith(('SELECT', 'SHOW')) for q, _ in engine.queries))
            failures = [Engine('wrong'), Engine(db, tls=False), Engine(db, head='wrong'), Engine(db, count=58)]
            for engine in failures:
                with self.subTest(database=db), self.assertRaises(RuntimeError):
                    target.verify_tracking(engine, db)
                self.assertTrue(engine.disposed)

    def test_legacy_launcher_child_command_and_private_uri(self):
        child = Mock(stdout=iter(['healthy\n'])); child.wait.return_value = 0
        with patch('sqlalchemy.create_engine', return_value=Engine('mlflow')), \
             patch.object(launcher.subprocess, 'Popen', return_value=child) as start, \
             patch.object(launcher.signal, 'signal'), patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(launcher.main(), 0)
        command = start.call_args.args[0]
        self.assertEqual(command, [sys.executable, '-m', 'mlflow', 'server', '--host', '0.0.0.0',
                         '--port', '5000', '--workers', '1', '--serve-artifacts',
                         '--artifacts-destination', '/mlflow/artifacts', '--default-artifact-root',
                         'mlflow-artifacts:/', '--allowed-hosts', BASE['MLFLOW_SERVER_ALLOWED_HOSTS'],
                         '--cors-allowed-origins', 'http://localhost:5000,http://127.0.0.1:5000'])
        for value in [BASE['DB_HOST'], BASE['DB_PASSWORD'], BASE['DB_USER']]:
            self.assertNotIn(value, str(command))
        self.assertEqual(make_url(start.call_args.kwargs['env']['MLFLOW_BACKEND_STORE_URI']).database, 'mlflow')

    def test_wrong_target_never_creates_engine_or_starts_server(self):
        with patch.dict(os.environ, {**dev_env(), 'DB_NAME': 'mlflow'}, clear=True), \
             patch('sqlalchemy.create_engine') as engine, patch.object(launcher.subprocess, 'Popen') as start:
            with self.assertRaises(RuntimeError): launcher.main()
            engine.assert_not_called(); start.assert_not_called()

    def test_redaction_covers_raw_and_encoded_values(self):
        env = {**BASE, 'MLFLOW_FLASK_SERVER_SECRET_KEY': 'synthetic-flask-value',
               'MLFLOW_TRACKING_TOKEN': 'synthetic-token-value'}
        values = target.sensitive_values(env)
        for value in [BASE['DB_HOST'], BASE['DB_USER'], BASE['DB_PASSWORD'], env['MLFLOW_FLASK_SERVER_SECRET_KEY'], env['MLFLOW_TRACKING_TOKEN']]:
            for encode in [lambda v: v, lambda v: quote(v, safe=''), lambda v: quote_plus(v, safe='')]:
                self.assertNotIn(encode(value), target.redact('error ' + encode(value), values))

    def test_optimized_python_still_rejects_wrong_db_without_secret_logs(self):
        env = {**BASE, 'DB_NAME': 'wrong_database', 'MLFLOW_DISABLE_AGENT_HINT': '1',
               'MLFLOW_DISABLE_TELEMETRY': 'true'}
        result = subprocess.run([sys.executable, '-O', str(CODE/'launcher.py')],
                                env=env, text=True, capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('MLflow startup failed', result.stderr)
        for value in [BASE['DB_HOST'], BASE['DB_USER'], BASE['DB_PASSWORD'], 'wrong_database']:
            self.assertNotIn(value, result.stdout + result.stderr)


class AuthTests(Offline):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.policy = {'mode': 'production', 'expected_host': BASE['DB_HOST'],
                       'expected_database': 'mlflow_auth_prod_v1', 'expected_head': auth_config.HEAD,
                       'tls_required': True, 'ca_file': '/fixture/ca.pem', 'allow_empty': True}
        self.secret = {'host': BASE['DB_HOST'], 'port': 3306, 'database': 'mlflow_auth_prod_v1',
                       'username': 'auth_fixture', 'password': 'synthetic-auth/+@ value'}
        os.environ.update(AUTH_POLICY=str(self.root/'policy.json'), AUTH_DB_SECRET=str(self.root/'connection.json'),
                          MLFLOW_FLASK_SERVER_SECRET_KEY='synthetic-flask-value')

    def save(self):
        (self.root/'policy.json').write_text(json.dumps(self.policy))
        (self.root/'connection.json').write_text(json.dumps(self.secret))

    def development(self):
        os.environ.update(dev_env(), MLFLOW_EXPECTED_AUTH_HOST=BASE['DB_HOST'],
                          MLFLOW_EXPECTED_AUTH_USER=self.secret['username'])
        self.policy.update(mode='development', expected_database='mlflow_auth_dev')
        self.secret['database'] = 'mlflow_auth_dev'

    def test_legacy_production_auth_policy_compatible(self):
        self.save(); policy, secret = auth_config.load()
        self.assertEqual((policy, secret), (self.policy, self.secret))
        url = make_url(auth_config.uri(policy, secret))
        self.assertEqual(url.database, secret['database']); self.assertEqual(url.query['charset'], 'utf8mb4')

    def test_dev_policy_and_connection_are_mysql_tls(self):
        self.development(); self.save(); policy, secret = auth_config.load()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with patch.object(auth_config.ssl, 'create_default_context', return_value=context) as create:
            args = auth_config.connect_args(policy, secret)
        create.assert_called_once_with(cafile=self.policy['ca_file'])
        self.assertTrue(args['ssl'].check_hostname)
        self.assertEqual(args['ssl'].verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(args['database'], 'mlflow_auth_dev')
        self.assertEqual(make_url(auth_config.uri(policy, secret)).drivername, 'mysql+pymysql')

    def test_legacy_isolated_test_mode_is_not_a_sqlite_escape(self):
        self.policy['mode'] = 'isolated-test'; self.save()
        with self.assertRaises(RuntimeError): auth_config.load()

    def test_wrong_auth_identity_or_head_rejected(self):
        self.development()
        for key, value in [('host', 'wrong.example.invalid'), ('database', 'mlflow'),
                           ('username', 'wrong_user'), ('port', 3307)]:
            original = self.secret[key]; self.secret[key] = value; self.save()
            with self.subTest(key=key), self.assertRaises(RuntimeError): auth_config.load()
            self.secret[key] = original
        self.policy['expected_head'] = 'wrong'; self.save()
        with self.assertRaises(RuntimeError): auth_config.load()

    def test_concrete_auth_policy_cannot_disagree_with_environment(self):
        self.development()
        self.policy['expected_host'] = 'stale.example.invalid'
        self.save()
        with self.assertRaises(RuntimeError): auth_config.load()

    def test_no_tls_downgrade_for_load_driver_or_uri(self):
        self.policy['tls_required'] = False; self.save()
        for call in [auth_config.load, lambda: auth_config.uri(self.policy, self.secret),
                     lambda: auth_config.connect_args(self.policy, self.secret)]:
            with self.assertRaises(RuntimeError): call()

    def test_missing_dev_expectations_and_wrong_mode_rejected(self):
        self.development(); self.save()
        for key in ['MLFLOW_EXPECTED_AUTH_HOST', 'MLFLOW_EXPECTED_AUTH_USER', 'MLFLOW_ENV']:
            value = os.environ.pop(key)
            with self.subTest(key=key), self.assertRaises(RuntimeError): auth_config.load()
            os.environ[key] = value

    def prepare(self, **auth_options):
        self.save()
        auth = Engine(self.secret['database'], head=auth_config.HEAD, auth=True, **auth_options)
        tracking = Engine(os.environ['DB_NAME'])
        with patch.object(auth_server, 'create_engine', side_effect=[auth, tracking]), \
             patch.object(auth_server, 'write_auth_config', return_value='/fixture/auth.ini') as write:
            prepared = auth_server.prepare()
        return prepared, auth, tracking, write

    def test_prod_and_dev_share_server_path_without_sqlite(self):
        for dev in [False, True]:
            if dev: self.development()
            with self.subTest(dev=dev):
                (cmd, env, values), auth, tracking, write = self.prepare()
                self.assertEqual(make_url(env['MLFLOW_BACKEND_STORE_URI']).database, os.environ['DB_NAME'])
                self.assertNotIn('sqlite', env['MLFLOW_BACKEND_STORE_URI'])
                self.assertEqual(cmd[cmd.index('--artifacts-destination')+1], '/mlflow/artifacts')
                self.assertTrue(auth.disposed and tracking.disposed)
                write.assert_called_once()
                self.assertEqual(cmd[cmd.index('--app-name')+1], 'basic-auth')
                for credential in [self.secret['password'], BASE['DB_PASSWORD'], os.environ['MLFLOW_FLASK_SERVER_SECRET_KEY']]:
                    self.assertNotIn(credential, str(cmd))
                    self.assertNotIn(credential, target.redact(credential, values))

    def test_auth_server_requires_admin_and_live_tls(self):
        for options in [{'admin': 0}, {'tls': False}]:
            with self.subTest(options=options), self.assertRaises(RuntimeError): self.prepare(**options)

    def test_cross_target_before_connection_or_config_write(self):
        self.development(); self.save(); os.environ['DB_NAME'] = 'mlflow'
        with patch.object(auth_server, 'create_engine') as engine, patch.object(auth_server, 'write_auth_config') as write:
            with self.assertRaises(RuntimeError): auth_server.prepare()
            engine.assert_not_called(); write.assert_not_called()

    def test_bootstrap_password_must_not_reach_server(self):
        self.save(); os.environ['MLFLOW_AUTH_ADMIN_PASSWORD'] = 'synthetic-bootstrap'
        with patch.object(auth_server, 'create_engine') as engine:
            with self.assertRaises(RuntimeError): auth_server.prepare()
            engine.assert_not_called()

    def test_child_logs_redact_auth_and_tracking_secrets(self):
        self.save()
        cmd, env, values = self.prepare()[0]
        child = Mock(stdout=iter([self.secret['password']+' '+BASE['DB_PASSWORD']+'\n'])); child.wait.return_value = 0
        with patch.object(auth_server, 'prepare', return_value=(cmd, env, values)), \
             patch.object(auth_server.subprocess, 'Popen', return_value=child), \
             patch.object(auth_server.signal, 'signal'), patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(auth_server.main(), 0)
        self.assertNotIn(self.secret['password'], out.getvalue()); self.assertNotIn(BASE['DB_PASSWORD'], out.getvalue())


if __name__ == '__main__': unittest.main()
