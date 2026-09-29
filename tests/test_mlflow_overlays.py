"""Offline manifest compatibility and reviewed migration preservation checks."""
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def render(path):
    result = subprocess.run(['kubectl', 'kustomize', str(ROOT/path)],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise AssertionError(result.stderr)
    return [x for x in yaml.safe_load_all(result.stdout) if x]


def index(objects):
    return {(x['kind'], x['metadata'].get('namespace'), x['metadata']['name']): x for x in objects}


class PreservationTests(unittest.TestCase):
    def test_reviewed_bootstrap_and_revision_hashes_unchanged(self):
        expected = {'bootstrap_admin.py': '5f70e516a1e372e3440b825da2d16354bb01d838780682682a0efc3c0a1b9e84',
                    'migration-hashes.json': '9c809e535b2e8d129a5bbcc78d29aff6d4803cea5a1246a3ad1aa0a738a9fe22'}
        for name, digest in expected.items():
            with self.subTest(file=name):
                self.assertEqual(hashlib.sha256((ROOT/'k8s/mlflow/auth'/name).read_bytes()).hexdigest(), digest)

    def test_reviewed_fence_unchanged_except_schema_snapshot(self):
        tree = ast.parse((ROOT/'k8s/mlflow/auth/fenced_migration.py').read_text())
        tree.body = [node for node in tree.body
                     if not (isinstance(node, ast.FunctionDef) and node.name == 'snapshot')]
        self.assertEqual(hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest(),
                         'fdaf4819baab14b1a994a8967424ea6be955f0071e5ad040daa4eb0b34ed871f')

    def test_api_compatibility_copies_do_not_drift(self):
        for name in ['deployment.yaml', 'configmap.yaml', 'service.yaml', 'hpa.yaml']:
            with self.subTest(file=name):
                self.assertEqual((ROOT/'k8s'/name).read_bytes(), (ROOT/'k8s/base/api'/name).read_bytes())


@unittest.skipUnless(shutil.which('kubectl'), 'kubectl is required for offline kustomize rendering')
class OverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prod = index(render('k8s/overlays/prod'))
        cls.dev = index(render('k8s/overlays/dev'))

    def test_prod_identifiers_hpa_service_resources_and_replicas_preserved(self):
        for name in ['service.yaml', 'hpa.yaml']:
            original = yaml.safe_load((ROOT/'k8s'/name).read_text())
            self.assertEqual(self.prod[next(iter(index([original])))], original)
        for original in render('k8s/base'):
            key = next(iter(index([original])))
            if original['kind'] in ['Namespace', 'PersistentVolumeClaim', 'Service']:
                self.assertEqual(self.prod[key], original)
            if original['kind'] == 'Deployment':
                promoted = self.prod[key]
                for field in ['replicas', 'selector']:
                    self.assertEqual(promoted['spec'][field], original['spec'][field])
                self.assertEqual(promoted['spec']['template']['spec']['containers'][0]['resources'],
                                 original['spec']['template']['spec']['containers'][0]['resources'])

    def test_shared_auth_code_images_and_both_environment_jobs_suspended(self):
        codes = []
        for objects, ns in [(self.dev, 'mlflow-dev'), (self.prod, 'mlflow')]:
            self.assertFalse(any(key[0] == 'Secret' for key in objects))
            server = objects[('Deployment', ns, 'mlflow')]['spec']['template']['spec']['containers'][0]
            self.assertEqual(server['command'], ['python', '-B', '/opt/mlflow/auth/runtime_guard.py'])
            self.assertNotIn('MLFLOW_AUTH_ADMIN_PASSWORD', [x['name'] for x in server['env']])
            jobs = [v for k, v in objects.items() if k[0] == 'Job']
            self.assertEqual(len(jobs), 2)
            for job in jobs:
                spec = job['spec']
                self.assertIs(spec['suspend'], True)
                self.assertEqual((spec['backoffLimit'], spec['parallelism'], spec['completions']), (0, 1, 1))
                self.assertEqual(spec['activeDeadlineSeconds'], 300)
                pod = spec['template']['spec'];self.assertEqual(pod['restartPolicy'], 'Never')
                container = pod['containers'][0];self.assertEqual(container['image'], server['image'])
                env = {x['name']: x for x in container['env']}
                expected = ('MLFLOW_EXPECTED_AUTH_MIGRATION_USER' if container['name'] == 'migration'
                            else 'MLFLOW_EXPECTED_AUTH_RUNTIME_USER')
                self.assertEqual(env['MLFLOW_EXPECTED_AUTH_USER']['valueFrom']['secretKeyRef'],
                                 {'name': 'mlflow-target-policy', 'key': expected})
                self.assertEqual(env['MLFLOW_ENV']['valueFrom']['configMapKeyRef'],
                                 {'name': 'mlflow-config', 'key': 'MLFLOW_ENV'})
            codes.append(next(v['data'] for k,v in objects.items() if k[0] == 'ConfigMap'
                              and k[2].startswith('mlflow-auth-code-')))
        self.assertEqual(codes[0], codes[1])

    def test_prod_secret_contract_covers_every_reference_without_values(self):
        contract = json.loads((ROOT/'k8s/overlays/prod/secret-contracts.json').read_text())
        secrets = {(s['namespace'], s['name']): set(s['required_keys']) for s in contract['secrets']}
        for key, obj in self.prod.items():
            if key[0] not in ['Deployment', 'Job']: continue
            pod = obj['spec']['template']['spec']
            for c in pod['containers']:
                for env in c.get('env', []):
                    ref = env.get('valueFrom', {}).get('secretKeyRef')
                    if ref:
                        self.assertIn(ref['key'], secrets[(key[1], ref['name'])])
                        self.assertFalse(ref.get('optional', False))
                for env in c.get('envFrom', []):
                    if 'secretRef' in env:
                        self.assertIn((key[1], env['secretRef']['name']), secrets)
                        self.assertFalse(env['secretRef'].get('optional', False))
            for volume in pod.get('volumes', []):
                if 'secret' in volume:
                    ref = volume['secret']
                    self.assertIn('connection.json', secrets[(key[1], ref['secretName'])])
                    self.assertFalse(ref.get('optional', False))
        self.assertEqual(contract['auth_database'], 'mlflow_auth_prod_v1')

    def test_environment_policy_tls_and_client_are_explicit_and_equal_in_strength(self):
        for objects, mode, ns, api_ns, tracking, auth in [
            (self.dev, 'dev', 'mlflow-dev', 'datacenter-app-dev', 'mlflow_tracking_dev', 'mlflow_auth_dev'),
            (self.prod, 'prod', 'mlflow', 'datacenter-app', 'mlflow', 'mlflow_auth_prod_v1')]:
            config = objects[('ConfigMap', ns, 'mlflow-config')]['data']
            self.assertEqual(config['MLFLOW_ENV'], mode)
            self.assertEqual(config['DB_NAME'], tracking)
            self.assertEqual(config['MLFLOW_EXPECTED_DB_NAME'], tracking)
            policies = [v['data'] for k,v in objects.items() if k[0] == 'ConfigMap'
                        and k[2].startswith('mlflow-auth-policy-')]
            self.assertEqual(len(policies), 1)
            policy = json.loads(policies[0]['policy.json'])
            self.assertEqual(policy['expected_database'], auth)
            self.assertEqual(policy['expected_head'], 'f1a2b3c4d5e6')
            self.assertIs(policy['tls_required'], True)
            self.assertEqual(json.loads(policies[0]['checkpoints.json']), [])
            api = objects[('Deployment', api_ns, 'datacenter-api')]['spec']['template']['spec']['containers'][0]
            env = {e['name']: e for e in api['env']}
            for name in ['MLFLOW_TRACKING_USERNAME', 'MLFLOW_TRACKING_PASSWORD']:
                self.assertEqual(env[name]['valueFrom']['secretKeyRef'],
                                 {'name': 'datacenter-mlflow-client', 'key': name})
            config = objects[('ConfigMap', api_ns, 'datacenter-api-config')]['data']
            self.assertEqual(config['MLFLOW_ENV'], mode)
            self.assertEqual(config['MODEL_SOURCE'], 'registry')
            self.assertEqual(config['MLFLOW_MODEL_VERSION'], '1')
            self.assertEqual(config['MLFLOW_MODEL_NAME'], 'datacenter-anomaly-detector')
            self.assertEqual(config['MLFLOW_TRACKING_URI'], 'http://mlflow.'+ns+'.svc.cluster.local:5000')
            self.assertTrue(config['DB_SSL_CA'])
            self.assertEqual('DIFY_API_KEY' in env, mode == 'prod')

    def test_all_configmap_and_ca_mounts_resolve_in_both_overlays(self):
        for objects in [self.dev, self.prod]:
            for key, obj in objects.items():
                if key[0] not in ['Deployment', 'Job']: continue
                pod = obj['spec']['template']['spec']
                volumes = {v['name']: v for v in pod['volumes']}
                for c in pod['containers']:
                    for mount in c.get('volumeMounts', []):
                        volume = volumes[mount['name']]
                        if 'configMap' not in volume: continue
                        data = objects[('ConfigMap', key[1], volume['configMap']['name'])]['data']
                        if mount.get('subPath'): self.assertIn(mount['subPath'], data)
                        if mount['mountPath'] in ['/etc/mlflow/certs', '/etc/datacenter/certs']:
                            self.assertIn('-----BEGIN CERTIFICATE-----', data['global-bundle.pem'])
                            self.assertNotIn('PRIVATE KEY', data['global-bundle.pem'])

    def test_auth_imports_with_projected_configmap_symlinks(self):
        objects = [(mapping, obj) for mapping, ns in [(self.dev, 'mlflow-dev'), (self.prod, 'mlflow')]
                   for key, obj in mapping.items() if key[1] == ns and key[0] in {'Deployment', 'Job'}]
        self.assertEqual(len(objects), 6)
        for mapping, obj in objects:
            with self.subTest(kind=obj['kind'], name=obj['metadata']['name']), tempfile.TemporaryDirectory() as tmp:
                pod = obj['spec']['template']['spec']
                container = pod['containers'][0]
                env = {item['name']: item.get('value') for item in container['env']}
                self.assertEqual(env['PYTHONPATH'], '/opt/mlflow:/opt/mlflow/auth')
                mounts = {item['mountPath']: item for item in container['volumeMounts']}
                self.assertNotIn('/opt/mlflow', mounts)
                shared = mounts['/opt/mlflow/db_target.py']
                self.assertEqual(shared['subPath'], 'db_target.py')
                self.assertIs(shared['readOnly'], True)
                self.assertIs(mounts['/opt/mlflow/auth']['readOnly'], True)
                volumes = {item['name']: item for item in pod['volumes']}
                def data(mount):
                    name = volumes[mount['name']]['configMap']['name']
                    return mapping[('ConfigMap', obj['metadata']['namespace'], name)]['data']
                # Model Kubernetes' ..data timestamp projection, plus a subPath file.
                root = Path(tmp)/'opt/mlflow'
                auth = root/'auth'
                timestamp = auth/'..2026_09_25_00_00_00.000000001'
                timestamp.mkdir(parents=True)
                (auth/'..data').symlink_to(timestamp.name, target_is_directory=True)
                for name, content in data(mounts['/opt/mlflow/auth']).items():
                    (timestamp/name).write_text(content)
                    (auth/name).symlink_to(Path('..data')/name)
                (root/'db_target.py').write_text(data(shared)['db_target.py'])
                policy_mount = mounts['/opt/mlflow/environment-targets.json']
                self.assertEqual(policy_mount['subPath'], 'environment-targets.json')
                (root/'environment-targets.json').write_text(data(policy_mount)['environment-targets.json'])
                entrypoint = Path(container['command'][-1]).stem
                smoke = """
import importlib, socket, sys
from unittest.mock import patch
with patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')), \
     patch('pymysql.connect', side_effect=AssertionError('DB forbidden')), \
     patch('sqlalchemy.create_engine', side_effect=AssertionError('DB forbidden')):
    import db_target
    before = list(sys.path)
    import auth_config
    assert sys.path == before, 'Auth must not infer paths from projection symlinks'
    module = importlib.import_module(sys.argv[1])
    assert callable(module.main)
print('IMPORT PASS')
"""
                child_env = {**os.environ, 'PYTHONPATH': str(root)+os.pathsep+str(auth),
                             'MLFLOW_DISABLE_AGENT_HINT': '1', 'MLFLOW_DISABLE_TELEMETRY': 'true'}
                result = subprocess.run([sys.executable, '-B', '-c', smoke, entrypoint],
                                        cwd=tmp, env=child_env, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('IMPORT PASS', result.stdout)
                # Missing the common module path must fail before opening a DB.
                child_env['PYTHONPATH'] = str(auth)
                missing = subprocess.run([sys.executable, '-B', '-c', 'import auth_config'],
                                         cwd=tmp, env=child_env, capture_output=True, text=True, timeout=30)
                self.assertNotEqual(missing.returncode, 0)
                self.assertIn("No module named 'db_target'", missing.stderr)

    def test_dev_configmap_mounts_resolve_and_ca_is_public(self):
        for key, obj in self.dev.items():
            if key[0] not in {'Deployment', 'Job'}:
                continue
            namespace = key[1]
            pod = obj['spec']['template']['spec']
            volumes = {v['name']: v for v in pod['volumes']}
            for volume in volumes.values():
                if 'configMap' in volume:
                    self.assertIn(('ConfigMap', namespace, volume['configMap']['name']), self.dev)
            for container in pod['containers']:
                for mount in container.get('volumeMounts', []):
                    self.assertIn(mount['name'], volumes)
        for namespace in ['mlflow-dev', 'datacenter-app-dev']:
            cas = [v for k, v in self.dev.items() if k[0] == 'ConfigMap' and k[1] == namespace
                   and k[2].startswith('mlflow-rds-ca-')]
            self.assertEqual(len(cas), 1)
            bundle = cas[0]['data']['global-bundle.pem']
            self.assertIn('-----BEGIN CERTIFICATE-----', bundle)
            self.assertNotIn('PRIVATE KEY', bundle)

    def test_dev_namespaces_service_hpa_and_references(self):
        namespaces = {'mlflow-dev', 'datacenter-app-dev'}
        for key in self.dev:
            if key[0] == 'Namespace': self.assertIn(key[2], namespaces)
            else: self.assertIn(key[1], namespaces)
        self.assertFalse(any(key[0] == 'HorizontalPodAutoscaler' for key in self.dev))
        service = self.dev[('Service', 'datacenter-app-dev', 'datacenter-api')]
        self.assertEqual(service['spec']['type'], 'ClusterIP')
        api = self.dev[('Deployment', 'datacenter-app-dev', 'datacenter-api')]
        self.assertEqual(api['spec']['replicas'], 1)
        self.assertEqual(api['spec']['selector'], self.prod[('Deployment', 'datacenter-app', 'datacenter-api')]['spec']['selector'])
        container = api['spec']['template']['spec']['containers'][0]
        self.assertIn('@sha256:7f8e22800304d641930283c95be2b5d34325af8fab72f79b3851ff445e6b646e', container['image'])
        self.assertEqual(container['image'], self.prod[('Deployment', 'datacenter-app', 'datacenter-api')]['spec']['template']['spec']['containers'][0]['image'])
        self.assertNotIn('DIFY_API_KEY', [env['name'] for env in container['env']])
        for name in ['MLFLOW_TRACKING_USERNAME', 'MLFLOW_TRACKING_PASSWORD']:
            env = next(x for x in container['env'] if x['name'] == name)
            self.assertEqual(env['valueFrom']['secretKeyRef']['name'], 'datacenter-mlflow-client')
        appconfig = self.dev[('ConfigMap', 'datacenter-app-dev', 'datacenter-api-config')]['data']
        self.assertEqual(appconfig['DB_NAME'], 'datacenter_app_dev')
        self.assertEqual(appconfig['MLFLOW_TRACKING_URI'], 'http://mlflow.mlflow-dev.svc.cluster.local:5000')

    def test_dev_mysql_configuration_has_no_prod_fallback(self):
        config = self.dev[('ConfigMap', 'mlflow-dev', 'mlflow-config')]['data']
        self.assertEqual(config['MLFLOW_ENV'], 'dev')
        self.assertEqual(config['DB_NAME'], 'mlflow_tracking_dev')
        self.assertEqual(config['MLFLOW_EXPECTED_DB_NAME'], config['DB_NAME'])
        deployment = self.dev[('Deployment', 'mlflow-dev', 'mlflow')]
        env = deployment['spec']['template']['spec']['containers'][0]['env']
        expected = {'MLFLOW_EXPECTED_DB_HOST', 'MLFLOW_EXPECTED_DB_USER',
                    'MLFLOW_EXPECTED_AUTH_HOST', 'MLFLOW_EXPECTED_AUTH_USER'}
        self.assertTrue(expected.issubset({x['name'] for x in env}))
        for item in env:
            if item['name'] in expected:
                self.assertEqual(item['valueFrom']['secretKeyRef']['name'], 'mlflow-target-policy')
        api = self.dev[('Deployment', 'datacenter-app-dev', 'datacenter-api')]
        app_env = {x['name']: x for x in api['spec']['template']['spec']['containers'][0]['env']}
        for name in ['DB_EXPECTED_HOST', 'DB_EXPECTED_USER']:
            self.assertEqual(app_env[name]['valueFrom']['secretKeyRef'],
                             {'name': 'datacenter-target-policy', 'key': name})
        pvc = self.dev[('PersistentVolumeClaim', 'mlflow-dev', 'mlflow-artifacts')]
        self.assertEqual(pvc['metadata']['namespace'], 'mlflow-dev')


if __name__ == '__main__': unittest.main()
