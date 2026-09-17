"""Offline loader tests: no real Registry/DB calls or deployment changes."""
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np
import yaml
from sklearn.ensemble import RandomForestClassifier
from backend import model_loader as loader


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_default_local_preserves_joblib(self):
        with patch.object(loader.joblib, 'load', return_value='local') as load, patch.object(loader, '_load_registry') as remote:
            self.assertEqual(loader.load_model('models/model.pkl'), 'local')
            load.assert_called_once_with('models/model.pkl')
            remote.assert_not_called()

    def test_invalid_source_fails(self):
        os.environ['MODEL_SOURCE'] = 'other'
        with self.assertRaises(loader.ModelLoadError):
            loader.load_model('unused')

    def test_remote_failures_never_fallback_or_expose_message(self):
        os.environ['MODEL_SOURCE'] = 'registry'
        for stage in ['lookup', 'download', 'metadata', 'trust', 'load', 'features', 'classes']:
            with self.subTest(stage=stage), patch.object(loader, '_load_registry', side_effect=ValueError('sensitive-'+stage)), patch.object(loader.joblib, 'load') as local:
                with self.assertRaises(loader.ModelLoadError) as error:
                    loader.load_model('unused')
                self.assertNotIn('sensitive', str(error.exception))
                local.assert_not_called()

    def test_registry_failure_prevents_app_import(self):
        code = """
import os
from unittest.mock import patch
os.environ.update(MODEL_SOURCE='registry', DB_HOST='localhost',
                  DB_NAME='test', DB_USER='test', DB_PASSWORD='test')
from backend.model_loader import ModelLoadError
with patch('backend.model_loader._load_registry', side_effect=RuntimeError('private detail')):
    try:
        import backend.main
    except ModelLoadError as error:
        assert 'private detail' not in str(error)
    else:
        raise AssertionError('app initialized without a registry model')
"""
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fixed_version_and_internal_proxy(self):
        with patch('mlflow.MlflowClient') as client, patch('mlflow.artifacts.download_artifacts', return_value='/tmp/package') as download, patch.object(loader, '_load_package', return_value='registry'):
            client.return_value.get_model_version.return_value = SimpleNamespace(status='READY', version='1', source='mlflow-artifacts:/run/model')
            self.assertEqual(loader._load_registry(), 'registry')
            client.return_value.get_model_version.assert_called_once_with(loader.MODEL_NAME, '1')
            self.assertEqual(download.call_args.kwargs['artifact_uri'], 'models:/datacenter-anomaly-detector/1')
            self.assertEqual(download.call_args.kwargs['registry_uri'], loader.TRACKING_URI)
            client.return_value.get_model_version.return_value.source = 'file:///model'
            with self.assertRaises(loader.ModelLoadError):loader._load_registry()

    def test_unapproved_versions_and_uri_rejected(self):
        for key,value in [('MLFLOW_MODEL_VERSION','latest'),('MLFLOW_MODEL_VERSION','2'),('MLFLOW_MODEL_VERSION','@champion'),('MLFLOW_MODEL_NAME','other'),('MLFLOW_TRACKING_URI','http://external.invalid')]:
            with self.subTest(value=value), patch.dict(os.environ,{key:value}), patch('mlflow.MlflowClient') as client:
                with self.assertRaises(loader.ModelLoadError):loader._load_registry()
                client.assert_not_called()

    def model(self):
        m=RandomForestClassifier()
        m.feature_names_in_=np.array(loader.FEATURES)
        m.classes_=np.array([0,1])
        return m

    def test_feature_and_class_contract(self):
        m=self.model();self.assertIs(loader._validate_contract(m),m)
        m.feature_names_in_=np.array(loader.FEATURES[::-1])
        with self.assertRaises(loader.ModelLoadError):loader._validate_contract(m)
        m=self.model();m.classes_=np.array([1,0])
        with self.assertRaises(loader.ModelLoadError):loader._validate_contract(m)
        with self.assertRaises(loader.ModelLoadError):loader._validate_contract(object())

    def test_package_validates_before_deserialization(self):
        flavor={'serialization_format':'skops','skops_trusted_types':loader.TRUSTED_TYPES,'code':None,'pickled_model':'model.skops','sklearn_version':'1.9.0'}
        with TemporaryDirectory() as tmp, patch('skops.io.get_untrusted_types',return_value=loader.TRUSTED_TYPES) as types, patch('skops.io.load',return_value=self.model()) as load:
            def write(f):Path(tmp,'MLmodel').write_text(yaml.safe_dump({'flavors':{'sklearn':f}}))
            write(flavor);loader._load_package(tmp)
            self.assertEqual(load.call_args.kwargs['trusted'], ['sklearn.tree._tree.Tree'])
            for key,value in [('serialization_format','pickle'),('skops_trusted_types',['other.Type']),('code','custom.py'),('pickled_model','../model.skops'),('sklearn_version','0.0')]:
                load.reset_mock();write({**flavor,key:value})
                with self.assertRaises(loader.ModelLoadError):loader._load_package(tmp)
                load.assert_not_called()
            write(flavor);types.return_value=['sklearn.tree._tree.Tree','other.Type'];load.reset_mock()
            with self.assertRaises(loader.ModelLoadError):loader._load_package(tmp)
            load.assert_not_called()


if __name__ == '__main__':unittest.main()
