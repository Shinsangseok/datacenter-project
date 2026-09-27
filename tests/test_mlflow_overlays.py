"""Offline manifest compatibility and reviewed migration preservation checks."""
import ast
import hashlib
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

    def test_prod_api_matches_existing_files_exactly(self):
        for name in ['deployment.yaml', 'configmap.yaml', 'service.yaml', 'hpa.yaml']:
            original = yaml.safe_load((ROOT/'k8s'/name).read_text())
            self.assertEqual(self.prod[next(iter(index([original])))], original)

    def test_prod_mlflow_matches_existing_kustomize_entrypoint(self):
        original = index(render('k8s/mlflow'))
        for key, value in original.items(): self.assertEqual(self.prod[key], value)

    def test_prod_auth_unchanged_and_dev_jobs_suspended(self):
        for objects in [self.prod, self.dev]:
            self.assertFalse(any(key[0] == 'Secret' for key in objects))
        self.assertFalse(any(key[0] == 'Job' for key in self.prod))
        prod = self.prod[('Deployment', 'mlflow', 'mlflow')]
        self.assertEqual(prod['spec']['template']['spec']['containers'][0]['command'],
                         ['python', '/opt/mlflow/launcher.py'])
        dev = self.dev[('Deployment', 'mlflow-dev', 'mlflow')]
        server = dev['spec']['template']['spec']['containers'][0]
        self.assertEqual(server['command'], ['python', '-B', '/opt/mlflow/auth/runtime_guard.py'])
        self.assertNotIn('MLFLOW_AUTH_ADMIN_PASSWORD', [x['name'] for x in server['env']])
        jobs = [v for k, v in self.dev.items() if k[0] == 'Job']
        self.assertEqual(len(jobs), 2)
        for job in jobs:
            spec = job['spec']
            self.assertIs(spec['suspend'], True)
            self.assertEqual((spec['backoffLimit'], spec['parallelism'], spec['completions']), (0, 1, 1))
            self.assertEqual(spec['activeDeadlineSeconds'], 300)
            pod = spec['template']['spec']
            self.assertEqual(pod['restartPolicy'], 'Never')
            container = pod['containers'][0]
            self.assertEqual(container['image'], server['image'])
            env = {x['name']: x for x in container['env']}
            expected = ('MLFLOW_EXPECTED_AUTH_MIGRATION_USER' if container['name'] == 'migration'
                        else 'MLFLOW_EXPECTED_AUTH_RUNTIME_USER')
            self.assertEqual(env['MLFLOW_EXPECTED_AUTH_USER']['valueFrom']['secretKeyRef'],
                             {'name': 'mlflow-target-policy', 'key': expected})

    def test_auth_imports_with_projected_configmap_symlinks(self):
        objects = [obj for key, obj in self.dev.items()
                   if key[1] == 'mlflow-dev' and key[0] in {'Deployment', 'Job'}]
        self.assertEqual(len(objects), 3)
        for obj in objects:
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
                    return self.dev[('ConfigMap', 'mlflow-dev', name)]['data']
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
        self.assertIn('dev-candidate-not-built', container['image'])
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
