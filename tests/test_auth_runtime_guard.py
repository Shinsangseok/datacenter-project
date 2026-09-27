import copy
import configparser
import os
import tempfile
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

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

    def test_dev_enables_read_only_hook(self):
        self.assertEqual(self.check_mode('dev')['authorization_function'], 'read_only_auth:authenticate')

    def test_prod_retains_original_auth_function(self):
        self.assertNotIn('authorization_function', self.check_mode('prod'))


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
