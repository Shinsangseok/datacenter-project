"""Offline manifest compatibility and reviewed migration preservation checks."""
import hashlib
from pathlib import Path
import shutil
import subprocess
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
    def test_reviewed_fencing_bootstrap_and_revision_hashes_unchanged(self):
        expected = {'fenced_migration.py': '850fae6528af880ed9f00c72ff2956bf5df03963bbcdd9c60264a62271b61d86',
                    'bootstrap_admin.py': '5f70e516a1e372e3440b825da2d16354bb01d838780682682a0efc3c0a1b9e84',
                    'migration-hashes.json': '9c809e535b2e8d129a5bbcc78d29aff6d4803cea5a1246a3ad1aa0a738a9fe22'}
        for name, digest in expected.items():
            with self.subTest(file=name):
                self.assertEqual(hashlib.sha256((ROOT/'k8s/mlflow/auth'/name).read_bytes()).hexdigest(), digest)

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

    def test_auth_not_enabled_and_no_secret_values_rendered(self):
        for objects in [self.prod, self.dev]:
            self.assertFalse(any(key[0] in ['Secret', 'Job'] for key in objects))
            for key, value in objects.items():
                if key[0] == 'Deployment' and key[2] == 'mlflow':
                    self.assertEqual(value['spec']['template']['spec']['containers'][0]['command'],
                                     ['python', '/opt/mlflow/launcher.py'])

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
        self.assertEqual({x['name'] for x in env}, {'MLFLOW_EXPECTED_DB_HOST', 'MLFLOW_EXPECTED_DB_USER'})
        for item in env:
            self.assertEqual(item['valueFrom']['secretKeyRef']['name'], 'mlflow-target-policy')
        pvc = self.dev[('PersistentVolumeClaim', 'mlflow-dev', 'mlflow-artifacts')]
        self.assertEqual(pvc['metadata']['namespace'], 'mlflow-dev')


if __name__ == '__main__': unittest.main()
