import copy
import configparser
import os
import socket
import tempfile
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from flask import Flask, Response
from werkzeug.datastructures import Authorization

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'k8s/mlflow/auth'), str(ROOT/'k8s/mlflow')]
import runtime_guard as guard
import read_only_auth
import schema_fingerprint as sf


class RuntimeGuardTests(unittest.TestCase):
    def setUp(self):
        self.admin = (1, 'bootstrap-admin', 'scrypt:sample:hash', 1)
        self.expected = json.loads((ROOT/'k8s/mlflow/auth/expected-schema-v2.json').read_text())

    def test_expected_schema_is_independently_derived_from_reviewed_ddl(self):
        fixture = json.loads((ROOT/'tests/fixtures/mlflow_auth_schema.json').read_text())
        actual = {n: sf.fingerprint(sf.canonical_table(t['expected_ddl'], t['table'], t['columns'],
                  fixture['database_defaults'], fixture['catalog'])) for n, t in fixture['tables'].items()}
        self.assertEqual(actual, self.expected['tables'])

    def test_any_number_of_non_admin_clients_can_start(self):
        for count in (0, 1, 4, 30):
            rows = [self.admin] + [(i+2, 'client-'+str(i), 'scrypt:client:hash', 0) for i in range(count)]
            guard.validate_users(rows, 'bootstrap-admin')

    def test_missing_or_demoted_bootstrap_admin_rejected(self):
        for rows in ([], [(2, 'client', 'hash', 0)], [(1, 'bootstrap-admin', 'hash', 0)]):
            with self.assertRaises(RuntimeError): guard.validate_users(rows, 'bootstrap-admin')

    def test_other_or_additional_administrator_rejected(self):
        for rows in ([(2, 'different-admin', 'hash', 1)], [self.admin, (2, 'client', 'hash', 1)]):
            with self.assertRaises(RuntimeError): guard.validate_users(rows, 'bootstrap-admin')

    def test_duplicate_identity_or_empty_password_rejected(self):
        for row in [(2, 'BOOTSTRAP-ADMIN', 'hash', 0), (1, 'client', 'hash', 0),
                    (2, '', 'hash', 0), (2, 'client', '', 0), (2, 'client', 'hash', None)]:
            with self.assertRaises(RuntimeError): guard.validate_users([self.admin, row], 'bootstrap-admin')

    def test_exact_schema_and_revision_required(self):
        actual = self.expected['tables']
        guard.validate_schema(self.expected, actual, ['f1a2b3c4d5e6'])
        for revision in ([], ['unexpected'], ['f1a2b3c4d5e6', 'unexpected']):
            with self.assertRaises(RuntimeError): guard.validate_schema(self.expected, actual, revision)

    def test_schema_drift_or_missing_extra_table_rejected(self):
        for kind in ('missing', 'extra', 'drift'):
            actual = copy.deepcopy(self.expected['tables'])
            if kind == 'missing': actual.pop('users')
            elif kind == 'extra': actual['unexpected'] = actual['users']
            else: actual['users'] = sf.FORMAT+':'+('0'*64)
            with self.assertRaises(RuntimeError): guard.validate_schema(self.expected, actual, self.expected['revision'])

    def test_old_or_unversioned_hash_never_approved(self):
        for digest in ('0'*64, 'mysql-auth-schema-v1:'+('0'*64), sf.FORMAT+':bad'):
            expected = copy.deepcopy(self.expected);expected['tables']['users'] = digest
            with self.assertRaises(RuntimeError): guard.validate_schema(expected, expected['tables'], expected['revision'])


class RuntimeConnectionTests(unittest.TestCase):
    """Run the mandatory read-only startup path with synthetic DB responses."""
    SERVER = '11111111-2222-4333-8444-555555555555'

    def run_guard(self, database, *, actual_db=None, server=None, account='runtime@fixture',
                  role='NONE', tls=True, drift=False, users=None):
        expected = json.loads((ROOT/'k8s/mlflow/auth/expected-schema-v2.json').read_text())
        callbacks, queries = [], []
        class Result:
            def __init__(self, value): self.value = value
            def one(self): return self.value
            def scalar_one(self): return self.value
            def scalars(self): return self
            def all(self): return self.value
        class Connection:
            disposed = False
            def connect(self): return self
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def dispose(self): self.disposed = True
            def exec_driver_sql(self, sql):
                queries.append(sql)
                for callback in callbacks: callback(None, None, sql, None, None, False)
                if sql.startswith('SELECT DATABASE()'):
                    return Result((actual_db or database, server or RuntimeConnectionTests.SERVER, account, role))
                if 'Ssl_cipher' in sql: return Result(('Ssl_cipher', 'TLS' if tls else ''))
                if sql.startswith('SELECT TABLE_NAME'):
                    return Result([(n, 'BASE TABLE') for n in expected['tables']])
                if sql.startswith('SELECT COUNT(*)'): return Result(0)
                if sql.startswith('SHOW CREATE TABLE'): return Result(('fixture', 'fixture DDL'))
                if 'version_num' in sql: return Result(['f1a2b3c4d5e6'])
                if sql.startswith('SELECT id,username'):
                    return Result(users if users is not None else [(1, 'admin', 'hash', 1), (2, 'client', 'hash', 0)])
                raise AssertionError('Unexpected SQL')
        engine = Connection()
        def subscribe(*args):
            def register(fn): callbacks.append(fn); return fn
            return register
        def snapshot(conn, name, ddl, context):
            return {'schema': ('mysql-auth-schema-v2:' + '0'*64 if drift else expected['tables'][name])}
        policy = {'expected_server_uuid': self.SERVER, 'tls_required': True, 'ca_file': '/fixture/ca.pem'}
        secret = {'host': 'db.example.invalid', 'port': 3306, 'username': 'runtime',
                  'password': 'synthetic', 'database': database}
        with patch.dict(os.environ, {'MLFLOW_AUTH_ADMIN_USERNAME': 'admin'}, clear=True), \
             patch.object(guard, 'create_engine', return_value=engine), \
             patch.object(guard.event, 'listens_for', side_effect=subscribe), \
             patch.object(guard, 'load_context', return_value={}), \
             patch.object(guard, 'schema_snapshot', side_effect=snapshot):
            try:
                guard.verify_runtime(policy, secret)
            finally:
                self.assertTrue(engine.disposed)
                self.assertTrue(all(sql.startswith(('SELECT', 'SHOW')) for sql in queries))
        self.assertEqual(len(callbacks), 1)
        with self.assertRaises(RuntimeError): callbacks[0](None, None, 'CREATE TABLE forbidden', None, None, False)

    def test_both_databases_require_full_schema_and_admin_validation(self):
        for db in ['mlflow_auth_dev', 'mlflow_auth_prod_v1']:
            with self.subTest(database=db): self.run_guard(db)

    def test_actual_identity_and_tls_mismatch_rejected_in_both_environments(self):
        failures = [{'actual_db': 'mlflow_auth'}, {'server': 'different'}, {'account': 'other@fixture'},
                    {'role': 'unexpected_role'}, {'tls': False}]
        for db in ['mlflow_auth_dev', 'mlflow_auth_prod_v1']:
            for failure in failures:
                with self.subTest(database=db, failure=failure), self.assertRaises(RuntimeError):
                    self.run_guard(db, **failure)

    def test_semantic_admin_and_fingerprint_reject_in_both_environments(self):
        for db in ['mlflow_auth_dev', 'mlflow_auth_prod_v1']:
            for failure in [{'drift': True}, {'users': []}, {'users': [(1, 'admin', 'hash', 0)]}]:
                with self.subTest(database=db, failure=failure), self.assertRaises(RuntimeError):
                    self.run_guard(db, **failure)


class ReadOnlyAuthTests(unittest.TestCase):
    def setUp(self): self.app = Flask(__name__)

    def request(self, method, is_admin=False, invalid=False, path='/api/2.0/mlflow/experiments/get'):
        auth = Response('Unauthorized', 401) if invalid else Authorization('basic', {'username': 'client'})
        module = SimpleNamespace(authenticate_request_basic_auth=lambda: auth,
                                 store=SimpleNamespace(get_user=lambda u: SimpleNamespace(is_admin=is_admin)))
        with patch.dict(sys.modules, {'mlflow.server.auth': module}), self.app.test_request_context(path, method=method):
            return read_only_auth.authenticate()

    def test_valid_read_defers_resource_authorization_to_mlflow(self):
        for method in ('GET', 'HEAD'):
            self.assertIsInstance(self.request(method), Authorization)

    def test_all_write_methods_rejected_for_non_admin(self):
        for method in ('POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'):
            self.assertEqual(self.request(method).status_code, 403)

    def test_admin_can_use_official_management_api(self):
        self.assertIsInstance(self.request('POST', is_admin=True), Authorization)

    def test_invalid_credentials_remain_401_before_method_gate(self):
        for method in ('GET', 'POST', 'PUT'):
            self.assertEqual(self.request(method, invalid=True).status_code, 401)

    def test_client_management_and_unknown_get_routes_are_denied(self):
        for path in ['/api/2.0/mlflow/users/list', '/api/2.0/mlflow/users/get',
                     '/api/3.0/mlflow/roles/list', '/api/3.0/mlflow/roles/get',
                     '/ajax-api/2.0/mlflow/users/list', '/api/2.0/mlflow/experiments/create', '/unknown']:
            with self.subTest(path=path):
                self.assertEqual(self.request('GET', path=path).status_code, 403)

    def test_admin_management_reads_still_reach_mlflow_authorization(self):
        self.assertIsInstance(self.request('GET', is_admin=True, path='/api/2.0/mlflow/users/list'), Authorization)

    def test_model_read_and_artifact_proxy_paths_defer_to_resource_grants(self):
        for path in read_only_auth.READ_PATHS | {'/api/2.0/mlflow-artifacts/artifacts/2/run/artifacts/model.skops'}:
            self.assertIsInstance(self.request('GET', path=path), Authorization)

    def test_unknown_auth_return_type_fails_closed(self):
        module = SimpleNamespace(authenticate_request_basic_auth=lambda: None, store=None)
        with patch.dict(sys.modules, {'mlflow.server.auth': module}), self.app.test_request_context('/'):
            self.assertEqual(read_only_auth.authenticate().status_code, 401)


class MlflowPackageCompatibilityTests(unittest.TestCase):
    """Exercise the installed pinned package, with all data access mocked.

    The method gate alone is insufficient: native artifact routes must invoke the
    custom authorizer and still enforce MLflow's resource permission decision.
    No auth app creation, schema initialization, or network access is allowed.
    """
    @classmethod
    def setUpClass(cls):
        with patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')):
            import mlflow
            import mlflow.server.auth as auth
        cls.mlflow, cls.auth = mlflow, auth

    def setUp(self):
        blocker = patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden'))
        blocker.start()
        self.addCleanup(blocker.stop)
        self.user = SimpleNamespace(id=2, username='fixture-client', is_admin=False)
        self.store = Mock()
        self.store.authenticate_user.return_value = True
        self.store.get_user.return_value = self.user
        self.header = Authorization('basic', {'username': self.user.username,
                                            'password': 'synthetic-test-password'}).to_header()
        for target, value in [('store', self.store), ('_USER_AUTH_CACHE', None),
                              ('auth_config', self.auth.auth_config._replace(
                                  authorization_function='read_only_auth:authenticate'))]:
            replacement = patch.object(self.auth, target, value)
            replacement.start()
            self.addCleanup(replacement.stop)

    def test_pinned_auth_package_version(self):
        self.assertEqual(self.mlflow.__version__, '3.16.1')

    def test_real_basic_auth_preserves_read_and_rejects_write(self):
        for method, expected in [('GET', Authorization), ('PUT', Response)]:
            with self.auth.app.test_request_context(
                    '/api/2.0/mlflow/experiments/get', method=method,
                    headers={'Authorization': self.header}):
                result = self.auth.authenticate_request()
            self.assertIsInstance(result, expected)
            if method == 'PUT':
                self.assertEqual(result.status_code, 403)
        self.store.authenticate_user.assert_called_with('fixture-client', 'synthetic-test-password')
        self.store.init_db.assert_not_called()

    def test_granted_flask_reads_and_public_health_remain_available(self):
        path = '/api/2.0/mlflow/experiments/get'
        validator = Mock(return_value=True)
        with patch.dict(os.environ, {'MLFLOW_BASIC_AUTH_FAIL_CLOSED': 'true'}), \
             patch.dict(self.auth.BEFORE_REQUEST_VALIDATORS, {(path, 'GET'): validator}):
            with self.auth.app.test_request_context(
                    path, method='GET', headers={'Authorization': self.header}):
                self.assertIsNone(self.auth._before_request())
            validator.assert_called_once_with()
            self.store.reset_mock()
            with self.auth.app.test_request_context('/health', method='GET'):
                self.assertIsNone(self.auth._before_request())
            self.store.authenticate_user.assert_not_called()
            self.store.init_db.assert_not_called()

    def test_head_without_resource_validator_fails_closed(self):
        path = '/api/2.0/mlflow/experiments/get'
        validator = Mock(return_value=False)
        with patch.dict(os.environ, {'MLFLOW_BASIC_AUTH_FAIL_CLOSED': 'true'}), \
             patch.dict(self.auth.BEFORE_REQUEST_VALIDATORS, {(path, 'GET'): validator}):
            for method in ('GET', 'HEAD'):
                with self.subTest(method=method), self.auth.app.test_request_context(
                        path, method=method, headers={'Authorization': self.header}):
                    result = self.auth._before_request()
                    self.assertIsInstance(result, Response)
                    self.assertEqual(result.status_code, 403)
        # HEAD has no GET validator in this pinned package. Its denial must come
        # from the fail-closed fallback, rather than silently reaching a handler.
        validator.assert_called_once_with()

    def request_native_artifact(self, method='GET', *, allowed=True, header=True):
        from fastapi import FastAPI
        from starlette.responses import PlainTextResponse
        from starlette.testclient import TestClient
        app = FastAPI()
        path = '/api/2.0/mlflow-artifacts/artifacts/2/run/artifacts/model.skops'
        app.add_api_route(path, lambda: PlainTextResponse('fixture-artifact'), methods=['GET', 'PUT'])
        validator = AsyncMock(return_value=allowed)
        with patch.object(self.auth, '_find_fastapi_validator', return_value=validator), \
             patch.object(self.auth, 'resolve_workspace_for_request_if_enabled', return_value=None), \
             patch.object(self.auth, '_find_fastapi_after_request_handler', return_value=None), \
             patch.object(self.auth, '_find_fastapi_response_filter', return_value=None):
            self.auth.add_fastapi_permission_middleware(app)
            with TestClient(app) as client:
                response = client.request(method, path,
                                          headers={'Authorization': self.header} if header else {})
        self.store.init_db.assert_not_called()
        return response, validator

    def test_native_artifact_reads_still_require_resource_grants(self):
        for allowed, status in [(False, 403), (True, 200)]:
            with self.subTest(allowed=allowed):
                response, validator = self.request_native_artifact(allowed=allowed)
                self.assertEqual(response.status_code, status)
                validator.assert_awaited_once()

    def test_native_artifact_writes_rejected_before_resource_validator(self):
        response, validator = self.request_native_artifact(method='PUT')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.text, 'Read-only client')
        validator.assert_not_awaited()

    def test_native_missing_or_invalid_credentials_preserve_basic_challenge(self):
        for header in (False, True):
            with self.subTest(header=header):
                self.store.authenticate_user.return_value = False
                response, validator = self.request_native_artifact(header=header)
                self.assertEqual(response.status_code, 401)
                self.assertIn('Basic', response.headers['WWW-Authenticate'])
                validator.assert_not_awaited()


class AuthConfigModeTests(unittest.TestCase):
    def check_mode(self, mode):
        import auth_server
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'auth.ini'
            with patch.dict(os.environ, {'MLFLOW_ENV': mode}, clear=True), patch.object(auth_server, 'Path', return_value=target):
                auth_server.write_auth_config('placeholder')
            config = configparser.ConfigParser();config.read(target)
            self.assertEqual(config['mlflow']['default_permission'], 'NO_PERMISSIONS')
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            return config['mlflow']

    def test_server_forces_resource_authorization_gate_on(self):
        import auth_server
        secret = {'host': 'db.example.invalid', 'database': 'mlflow_auth_dev',
                  'username': 'auth-runtime', 'password': 'synthetic-test-password'}
        track = {'host': secret['host'], 'database': 'mlflow_tracking_dev',
                 'username': 'tracking-runtime'}
        for configured in ('false', 'true', ''):
            environment = {'MLFLOW_FLASK_SERVER_SECRET_KEY': 'synthetic-test-signing-key',
                           'MLFLOW_MYSQL_SSL_CA': '/fixture/ca.pem',
                           'MLFLOW_SERVER_ALLOWED_HOSTS': 'localhost:5000',
                           'MLFLOW_BASIC_AUTH_FAIL_CLOSED': configured}
            with self.subTest(configured=configured), \
                 patch.dict(os.environ, environment, clear=True), \
                 patch.object(auth_server, 'load', return_value=({}, secret)), \
                 patch.object(auth_server, 'tracking_connection', return_value=track), \
                 patch.object(auth_server, 'uri', return_value='synthetic-auth-uri'), \
                 patch.object(auth_server, 'mysql_uri', return_value='synthetic-tracking-uri'), \
                 patch.object(auth_server, 'verify_runtime') as runtime, \
                 patch.object(auth_server, 'create_engine'), \
                 patch.object(auth_server, 'verify_tracking') as tracking, \
                 patch.object(auth_server, 'write_auth_config', return_value='/fixture/auth.ini'):
                command, child_env, _ = auth_server.prepare()
            self.assertEqual(child_env['MLFLOW_BASIC_AUTH_FAIL_CLOSED'], 'true')
            self.assertIn('basic-auth', command)
            runtime.assert_called_once_with({}, secret)
            tracking.assert_called_once()

    def test_dev_enables_read_only_hook(self):
        self.assertEqual(self.check_mode('dev')['authorization_function'], 'read_only_auth:authenticate')

    def test_prod_uses_identical_read_only_hook(self):
        self.assertEqual(self.check_mode('prod')['authorization_function'], 'read_only_auth:authenticate')


class NetworkPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('dev_network', ROOT/'k8s/overlays/dev/network_policy.py')
        cls.module = importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.module)

    def policies(self): return self.module.policies(['198.51.100.8'], ['192.0.2.0/24'])

    def test_only_dev_namespaces_and_egress_are_selected(self):
        policies = self.policies()
        self.assertEqual({p['metadata']['namespace'] for p in policies}, {'mlflow-dev', 'datacenter-app-dev'})
        for p in policies:
            self.assertEqual(p['spec']['podSelector'], {})
            self.assertEqual(p['spec']['policyTypes'], ['Egress'])
            self.assertNotIn('ingress', p['spec'])

    def test_dns_namespace_and_pod_selectors_are_conjunctive(self):
        dns = self.policies()[0]['spec']['egress'][0]
        self.assertEqual(len(dns['to']), 1)
        self.assertEqual(set(dns['to'][0]), {'namespaceSelector', 'podSelector'})
        self.assertEqual(dns['ports'], [{'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53}])

    def test_no_prod_or_unrestricted_peer_and_only_dev_service_ports(self):
        peer = self.policies()[0]['spec']['egress'][1]
        self.assertEqual(peer['to'][0]['namespaceSelector']['matchExpressions'][0]['values'], ['mlflow-dev', 'datacenter-app-dev'])
        self.assertEqual({p['port'] for p in peer['ports']}, {5000, 8000})

    def test_rds_only_exact_addresses_on_mysql_port(self):
        rds = self.policies()[0]['spec']['egress'][2]
        self.assertEqual(rds['to'], [{'ipBlock': {'cidr': '198.51.100.8/32'}}])
        self.assertEqual(rds['ports'], [{'protocol': 'TCP', 'port': 3306}])

    def test_cluster_overlap_loopback_linklocal_and_ipv6_rejected(self):
        for ip in ('192.0.2.10', '127.0.0.1', '169.254.1.1', '0.0.0.0', '224.0.0.1', '::1'):
            with self.subTest(ip=ip), self.assertRaises(ValueError):
                self.module.policies([ip], ['192.0.2.0/24'])

    def test_empty_runtime_inputs_fail_closed(self):
        for ips, ranges in (([], ['192.0.2.0/24']), (['198.51.100.8'], [])):
            with self.assertRaises(ValueError): self.module.policies(ips, ranges)
