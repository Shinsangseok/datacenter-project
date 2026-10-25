"""Offline startup contracts. All SQL, Registry and inference writes are mocked."""
import errno
import json
import logging
import os
from pathlib import Path
import socket
import ssl
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

import pymysql
from pymysql import err as mysql
from sqlalchemy.exc import OperationalError, ProgrammingError, SQLAlchemyError
from requests import exceptions as req, Response
from mlflow.exceptions import MlflowException, RestException

from backend.startup import (Startup, StartupError, Policy, classify_db,
                             classify_registry, registry_transport, validate_database,
                             configure_database_timeouts)
from backend import model_loader as loader

SECRET = 'synthetic-password=never-log-this Authorization: Bearer fake-token mysql://fake:private@invalid/db'


class Clock:
    def __init__(self):
        self.now = 0.0
        self.waits = []
    def __call__(self):
        return self.now
    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def db_error(code=2003, inner=None):
    driver = mysql.OperationalError(code, SECRET)
    if inner is not None:
        driver.original_exception = inner
    return OperationalError('SELECT secret_fixture', {'secret': SECRET}, driver)


def registry_error(inner):
    error = MlflowException(SECRET)
    error.__cause__ = inner
    return error


class StartupTests(unittest.TestCase):
    def setUp(self):
        # Other suites initialize MLflow/Alembic logging in this process.
        # Isolate this log-contract fixture and restore its inherited state.
        logger = logging.getLogger('backend.startup')
        disabled, threshold = logger.disabled, logging.root.manager.disable
        logger.disabled = False
        logging.disable(logging.NOTSET)
        self.addCleanup(setattr, logger, 'disabled', disabled)
        self.addCleanup(logging.disable, threshold)
        self.clock = Clock()
        self.startup = Startup(clock=self.clock, sleep=self.clock.sleep)
        self.capture = self.assertLogs('backend.startup', level='INFO')
        self.logs = self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)
        # Ensure every test has a structured marker without depending on its failure branch.
        self.startup.run('configuration', lambda: None)

    def engine(self):
        engine = MagicMock()
        conn = engine.connect.return_value.__enter__.return_value
        return engine, conn

    def test_rds_connect_recovers_and_only_selects(self):
        engine, conn = self.engine()
        good = engine.connect.return_value
        engine.connect.side_effect = [db_error(inner=TimeoutError(SECRET)), good]
        validate_database(engine, self.startup)
        self.assertEqual(engine.connect.call_count, 2)
        self.assertEqual(self.clock.waits, [1])
        self.assertEqual(conn.execute.call_count, 1)
        self.assertTrue(str(conn.execute.call_args.args[0]).startswith('SELECT '))
        engine.begin.assert_not_called()

    def test_persistent_timeout_stops_after_three_attempts(self):
        engine, _ = self.engine()
        engine.connect.side_effect = db_error(inner=TimeoutError(SECRET))
        with self.assertRaises(StartupError) as caught:
            validate_database(engine, self.startup)
        self.assertEqual(caught.exception.reason, 'attempts_exhausted')
        self.assertEqual(engine.connect.call_count, 3)
        self.assertEqual(self.clock.waits, [1, 2])

    def test_schema_mismatch_fails_without_retry(self):
        engine, conn = self.engine()
        conn.execute.side_effect = ProgrammingError('SELECT hidden', {}, mysql.ProgrammingError(1054, SECRET))
        with self.assertRaises(StartupError) as caught:
            validate_database(engine, self.startup)
        self.assertEqual(caught.exception.failure.category, 'db_schema_or_target')
        self.assertEqual(engine.connect.call_count, 1)
        self.assertFalse(self.clock.waits)

    def test_auth_and_permission_fail_without_retry(self):
        for code in [1044, 1045, 1142, 1143, 1227, 1698]:
            engine, _ = self.engine()
            engine.connect.side_effect = db_error(code, TimeoutError(SECRET))
            with self.subTest(code=code), self.assertRaises(StartupError) as caught:
                validate_database(engine, self.startup)
            self.assertEqual(caught.exception.failure.category, 'db_auth_or_permission')
            self.assertEqual(engine.connect.call_count, 1)
        self.assertFalse(self.clock.waits)

    def test_mysql_number_alone_is_not_transient_proof(self):
        for code in [2002, 2003, 2006, 2013]:
            self.assertFalse(classify_db(db_error(code), 'db_connect').retryable)

    def test_unknown_driver_and_unknown_location_fail_closed(self):
        self.assertFalse(classify_db(OperationalError('', {}, OSError(errno.ECONNREFUSED, SECRET)), 'db_connect').retryable)
        self.assertFalse(classify_db(db_error(inner=TimeoutError()), 'predict_write').retryable)
        self.assertFalse(classify_registry(RuntimeError(SECRET), 'registry_lookup').retryable)
        self.assertFalse(classify_registry(req.Timeout(SECRET), 'model_load').retryable)

    def test_tls_failure_is_never_retried(self):
        self.assertFalse(classify_db(db_error(inner=ssl.SSLCertVerificationError(SECRET)), 'db_connect').retryable)
        self.assertFalse(classify_registry(registry_error(req.SSLError(SECRET)), 'registry_lookup').retryable)

    def test_dns_transient_vs_permanent(self):
        self.assertTrue(classify_db(db_error(inner=socket.gaierror(socket.EAI_AGAIN, SECRET)), 'db_connect').retryable)
        self.assertFalse(classify_db(db_error(inner=socket.gaierror(socket.EAI_NONAME, SECRET)), 'db_connect').retryable)

    def test_actual_pymysql_connect_preserves_typed_cause(self):
        conn = pymysql.Connection(host='fixture.invalid', defer_connect=True)
        with patch('pymysql.connections.socket.create_connection', side_effect=ConnectionRefusedError(errno.ECONNREFUSED, SECRET)) as network:
            try:
                conn.connect()
            except mysql.OperationalError as error:
                self.assertTrue(classify_db(error, 'db_connect').retryable)
            else:
                self.fail('Expected mocked connection failure')
        network.assert_called_once()

    def test_actual_pymysql_read_timeout_is_safe_select_retry(self):
        conn = pymysql.Connection(defer_connect=True)
        conn._sock = MagicMock()
        conn._rfile = MagicMock()
        conn._rfile.read.side_effect = TimeoutError(SECRET)
        try:
            conn._read_bytes(1)
        except mysql.OperationalError as error:
            self.assertEqual(error.args[0], 2013)
            self.assertTrue(classify_db(error, 'db_schema').retryable)
        else:
            self.fail('Expected mocked read timeout')

    def test_select_transport_failure_retries_whole_read_contract(self):
        engine, conn = self.engine()
        conn.execute.side_effect = [db_error(2013, TimeoutError(SECRET)), None]
        validate_database(engine, self.startup)
        self.assertEqual(engine.connect.call_count, 2)
        self.assertEqual(conn.execute.call_count, 2)
        engine.begin.assert_not_called()

    def test_unknown_exception_fails_once(self):
        engine, _ = self.engine()
        engine.connect.side_effect = RuntimeError(SECRET)
        with self.assertRaises(StartupError) as caught:
            validate_database(engine, self.startup)
        self.assertEqual(caught.exception.failure.category, 'db_unknown')
        self.assertEqual(engine.connect.call_count, 1)

    def test_logs_and_public_traceback_do_not_disclose_upstream_values(self):
        engine, _ = self.engine()
        engine.connect.side_effect = db_error(1045)
        try:
            validate_database(engine, self.startup)
        except StartupError:
            output = traceback.format_exc()
        self.assertNotIn(SECRET, output)
        for record in self.logs.records:
            payload = json.loads(record.getMessage())
            self.assertEqual(set(payload), {'component', 'startup_phase', 'error_category', 'retryable', 'attempt', 'max_attempts', 'elapsed_ms', 'exit_reason'})
            self.assertNotIn(SECRET, record.getMessage())
            self.assertIsNone(record.exc_info)

    def test_broken_classifier_is_sanitized_and_not_retried(self):
        operation = Mock(side_effect=RuntimeError(SECRET))
        classify = Mock(side_effect=ValueError(SECRET))
        with self.assertRaises(StartupError) as caught:
            self.startup.run('registry_lookup', operation, classify)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(caught.exception.failure.category, 'unknown')
        operation.assert_called_once()

    def test_shared_deadline_prevents_new_phase(self):
        self.startup.run('model_load', lambda: setattr(self.clock, 'now', 119))
        self.clock.now = 120
        operation = Mock()
        with self.assertRaises(StartupError) as caught:
            self.startup.run('db_contract', operation)
        operation.assert_not_called()
        self.assertEqual(caught.exception.reason, 'deadline_exceeded')

    def test_late_success_is_rejected(self):
        def operation():
            self.clock.now = 121
            return 'must-not-be-ready'
        with self.assertRaises(StartupError) as caught:
            self.startup.run('model_load', operation)
        self.assertEqual(caught.exception.reason, 'deadline_exceeded')

    def test_backoff_never_crosses_remaining_budget(self):
        self.clock.now = 119.5
        operation = Mock(side_effect=req.Timeout(SECRET))
        with self.assertRaises(StartupError):
            self.startup.run('registry_lookup', operation, classify_registry)
        operation.assert_called_once()
        self.assertFalse(self.clock.waits)

    def test_overslept_backoff_does_not_start_another_attempt(self):
        def oversleep(seconds):self.clock.now = 121
        self.startup.sleep = oversleep
        operation = Mock(side_effect=req.Timeout(SECRET))
        with self.assertRaises(StartupError):self.startup.run('registry_lookup', operation, classify_registry)
        operation.assert_called_once()

    def test_delay_cap_and_policy_validation(self):
        startup = Startup(Policy(base_delay=0.5, max_delay=0.5), clock=self.clock, sleep=self.clock.sleep)
        with self.assertRaises(StartupError):startup.run('registry_lookup', Mock(side_effect=req.Timeout()), classify_registry)
        self.assertEqual(self.clock.waits, [0.5, 0.5])
        for kwargs in [{'max_attempts': 4}, {'budget_seconds': float('inf')}, {'budget_seconds': 0}, {'base_delay': 5}, {'max_attempts': True}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):Policy(**kwargs)

    def test_http_status_and_registry_codes(self):
        for code in [401, 403, 404, 429, 500, 502, 503, 504]:
            response = Response(); response.status_code = code
            result = classify_registry(req.HTTPError(SECRET, response=response), 'registry_lookup')
            self.assertEqual(result.retryable, code in {502, 503, 504})
        for code in ['UNAUTHENTICATED', 'PERMISSION_DENIED', 'RESOURCE_DOES_NOT_EXIST', 'INVALID_PARAMETER_VALUE', 'INTERNAL_ERROR', 'TEMPORARILY_UNAVAILABLE']:
            result = classify_registry(RestException({'error_code': code, 'message': SECRET}), 'registry_lookup')
            self.assertEqual(result.retryable, code == 'TEMPORARILY_UNAVAILABLE')

    def test_unclassified_artifact_aggregate_is_not_retried(self):
        error = MlflowException('one or more artifacts failed: '+SECRET)
        operation = Mock(side_effect=error)
        with self.assertRaises(StartupError):self.startup.run('artifact_download', operation, classify_registry)
        operation.assert_called_once()

    def test_sdk_retry_settings_and_logging_restored_on_failure(self):
        saved_disable = logging.root.manager.disable
        with patch.dict(os.environ, {'MLFLOW_HTTP_REQUEST_MAX_RETRIES': '2'}):
            before = dict(os.environ)
            with self.assertRaises(req.Timeout):
                with registry_transport(self.startup):
                    self.assertEqual(os.environ['MLFLOW_HTTP_REQUEST_MAX_RETRIES'], '0')
                    self.assertEqual(os.environ['MLFLOW_HTTP_REQUEST_TIMEOUT'], '10')
                    self.assertEqual(os.environ['MLFLOW_ALLOW_HTTP_REDIRECTS'], 'false')
                    logging.getLogger('mlflow.sdk').error(SECRET)
                    raise req.Timeout(SECRET)
            self.assertEqual(dict(os.environ), before)
        self.assertEqual(logging.root.manager.disable, saved_disable)
        self.assertFalse(any(SECRET in r.getMessage() for r in self.logs.records))


    def test_timeout_caps_fractions_and_deadline(self):
        self.assertEqual(self.startup.timeout(3), 3)
        self.clock.now = 117.25
        self.assertEqual(self.startup.timeout(10), 2.75)
        self.assertEqual(self.startup.timeout(10, whole_seconds=True), 2)
        self.clock.now = 119.5
        self.assertEqual(self.startup.timeout(3), 0.5)
        with self.assertRaises(StartupError):
            self.startup.timeout(10, whole_seconds=True)
        self.clock.now = 120
        with self.assertRaises(StartupError): self.startup.timeout(3)

    def test_budget_guard_is_preserved_without_retry(self):
        self.clock.now = 119.5
        def lookup():
            with registry_transport(self.startup): self.fail('Must not enter transport')
        with self.assertRaises(StartupError) as caught:
            self.startup.run('registry_lookup', lookup, classify_registry)
        self.assertEqual(caught.exception.failure.category, 'startup_budget_exhausted')
        self.assertEqual(caught.exception.reason, 'deadline_exceeded')
        self.assertFalse(self.clock.waits)
        self.assertIn('deadline_exceeded', self.logs.records[-1].getMessage())

    def test_expired_connect_skips_schema_and_closes_connection(self):
        engine, conn = self.engine()
        good = engine.connect.return_value
        def connect(): self.clock.now = 120; return good
        engine.connect.side_effect = connect
        with self.assertRaises(StartupError) as caught:
            validate_database(engine, self.startup)
        self.assertEqual(caught.exception.reason, 'deadline_exceeded')
        conn.execute.assert_not_called()
        good.__exit__.assert_called_once()
        self.assertFalse(self.clock.waits)

    def test_sqlalchemy_recomputes_real_driver_connect_arguments_each_attempt(self):
        from sqlalchemy import create_engine
        from sqlalchemy.pool import NullPool
        engine = configure_database_timeouts(create_engine(
            'mysql+pymysql://fixture:synthetic@fixture.invalid/test', poolclass=NullPool,
            connect_args={'ssl': {'ca': '/fixture/ca.pem'}}, hide_parameters=True), self.startup)
        self.addCleanup(engine.dispose)
        self.clock.now = 110
        seen = []
        def connect(**kwargs):
            seen.append(kwargs)
            if len(seen) == 1: self.clock.now = 115
            error = mysql.OperationalError(2003, SECRET)
            error.original_exception = TimeoutError(SECRET)
            raise error
        with patch.object(engine.dialect.dbapi, 'connect', side_effect=connect):
            with self.assertRaises(StartupError) as caught:
                validate_database(engine, self.startup)
        self.assertEqual(caught.exception.reason, 'attempts_exhausted')
        self.assertEqual([x['read_timeout'] for x in seen], [10, 4, 2])
        self.assertEqual([x['write_timeout'] for x in seen], [10, 4, 2])
        self.assertEqual([x['connect_timeout'] for x in seen], [3, 3, 2])
        self.assertTrue(all(x['ssl'] == {'ca': '/fixture/ca.pem'} for x in seen))
        self.assertEqual(self.clock.waits, [1, 2])

    def test_schema_event_passes_remaining_timeout_to_real_pymysql_io(self):
        from sqlalchemy import create_engine
        from sqlalchemy.pool import NullPool
        engine = configure_database_timeouts(create_engine(
            'mysql+pymysql://fixture:synthetic@fixture.invalid/test', poolclass=NullPool), self.startup)
        self.addCleanup(engine.dispose)
        driver = pymysql.Connection(defer_connect=True)
        cursor = MagicMock()
        cursor.description = [('id', None, None, None, None, None, None)]
        driver.cursor = Mock(return_value=cursor)
        driver.rollback = Mock(); driver.close = Mock()
        def connect(**kwargs): self.clock.now = 118.75; return driver
        with patch.object(engine.dialect.dbapi, 'connect', side_effect=connect), patch.object(engine.dialect, 'initialize'):
            validate_database(engine, self.startup)
        self.assertEqual(driver._read_timeout, 1.25)
        self.assertEqual(driver._write_timeout, 1.25)
        driver._sock = MagicMock(); driver._rfile = MagicMock()
        driver._rfile.read.return_value = b'x'
        self.assertEqual(driver._read_bytes(1), b'x')
        driver._write_bytes(b'x')
        self.assertEqual(driver._sock.settimeout.call_args_list[0].args, (1.25,))
        self.assertEqual(driver._sock.settimeout.call_args_list[1].args, (1.25,))
        self.assertTrue(any('FROM measurements LIMIT 0' in str(x) for x in cursor.execute.call_args_list))
        driver._sock = None  # do not leave a fake live socket to __del__

    def test_incompatible_schema_driver_fails_closed(self):
        from sqlalchemy import create_engine
        from sqlalchemy.pool import NullPool
        engine = configure_database_timeouts(create_engine(
            'mysql+pymysql://fixture:synthetic@fixture.invalid/test', poolclass=NullPool), self.startup)
        self.addCleanup(engine.dispose)
        driver = MagicMock()
        with patch.object(engine.dialect.dbapi, 'connect', return_value=driver), patch.object(engine.dialect, 'initialize'):
            with self.assertRaises(StartupError) as caught:
                validate_database(engine, self.startup)
        self.assertEqual(caught.exception.failure.category, 'db_timeout_adapter_incompatible')
        self.assertFalse(self.clock.waits)

    def test_mlflow_timeout_reaches_requests_session_without_network(self):
        from mlflow.utils.rest_utils import http_request, MlflowHostCreds
        self.clock.now = 117.25
        with patch('mlflow.utils.request_utils._get_request_session') as session:
            session.return_value.request.return_value.status_code = 200
            with registry_transport(self.startup):
                http_request(MlflowHostCreds('http://fixture.invalid'), '/fixture', 'GET')
        self.assertEqual(session.return_value.request.call_args.kwargs['timeout'], 2)
        self.assertFalse(session.return_value.request.call_args.kwargs['allow_redirects'])
        self.assertEqual(session.call_args.args[0], 0)

    def test_airflow_os115_is_not_added_to_pymysql_allowlist(self):
        for phase in ['db_connect', 'db_schema']:
            result = classify_db(db_error(2002, OSError(errno.EINPROGRESS, SECRET)), phase)
            self.assertFalse(result.retryable)


class RegistryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'MODEL_SOURCE': 'registry'}, clear=True)
        self.env.start();self.addCleanup(self.env.stop)
        self.clock = Clock();self.startup = Startup(clock=self.clock, sleep=self.clock.sleep)
        self.entry = SimpleNamespace(status='READY', version='1', source='mlflow-artifacts:/run/model')

    def test_registry_timeout_then_model_loaded(self):
        with patch('mlflow.MlflowClient') as client, patch('mlflow.artifacts.download_artifacts', return_value='/tmp/mock-package') as download, patch.object(loader, '_load_package', return_value='safe-model') as package:
            client.return_value.get_model_version.side_effect = [registry_error(req.Timeout(SECRET)), self.entry]
            result = loader.load_model('unused', self.startup)
            self.assertEqual(result, 'safe-model')
            self.assertEqual(client.return_value.get_model_version.call_count, 2)
            download.assert_called_once();package.assert_called_once()
            self.assertEqual(self.clock.waits, [1])

    def test_registry_and_download_share_decreasing_budget(self):
        timeouts = []
        self.clock.now = 111
        def lookup(*args):
            timeouts.append(int(os.environ['MLFLOW_HTTP_REQUEST_TIMEOUT']))
            if len(timeouts) == 1:
                self.clock.now = 113
                raise req.Timeout(SECRET)
            self.clock.now = 117.5
            return self.entry
        def download(**kwargs):
            timeouts.append(int(os.environ['MLFLOW_HTTP_REQUEST_TIMEOUT']))
            return kwargs['dst_path']
        with patch('mlflow.MlflowClient') as client, patch('mlflow.artifacts.download_artifacts', side_effect=download), patch.object(loader, '_load_package', return_value='safe'):
            client.return_value.get_model_version.side_effect = lookup
            self.assertEqual(loader.load_model('unused', self.startup), 'safe')
        self.assertEqual(timeouts, [9, 6, 2])
        self.assertEqual(self.clock.waits, [1])

    def test_fractional_budget_blocks_download_before_sdk(self):
        def lookup(*args): self.clock.now = 119.5; return self.entry
        with patch('mlflow.MlflowClient') as client, patch('mlflow.artifacts.download_artifacts') as download, patch.object(loader, '_load_package') as load:
            client.return_value.get_model_version.side_effect = lookup
            with self.assertRaises(loader.ModelLoadError): loader.load_model('unused', self.startup)
            download.assert_not_called(); load.assert_not_called()
        self.assertFalse(self.clock.waits)

    def test_late_deserialization_is_rejected_without_retry(self):
        def package(path): self.clock.now = 121; return 'must-not-be-used'
        with patch('mlflow.MlflowClient') as client, patch('mlflow.artifacts.download_artifacts', return_value='/tmp/mock'), patch.object(loader, '_load_package', side_effect=package) as load:
            client.return_value.get_model_version.return_value = self.entry
            with self.assertRaises(loader.ModelLoadError): loader.load_model('unused', self.startup)
            load.assert_called_once()
        self.assertFalse(self.clock.waits)

    def test_missing_model_is_not_retried_and_never_downloaded(self):
        with patch('mlflow.MlflowClient') as client, patch('mlflow.artifacts.download_artifacts') as download:
            client.return_value.get_model_version.side_effect = RestException({'error_code':'RESOURCE_DOES_NOT_EXIST', 'message':SECRET})
            with self.assertRaises(loader.ModelLoadError):loader.load_model('unused', self.startup)
            client.return_value.get_model_version.assert_called_once();download.assert_not_called()

    def test_wrong_model_version_is_rejected_before_network(self):
        with patch.dict(os.environ, {'MLFLOW_MODEL_VERSION': '2'}), patch('mlflow.MlflowClient') as client:
            with self.assertRaises(loader.ModelLoadError):loader.load_model('unused', self.startup)
            client.assert_not_called()

    def test_invalid_returned_version_or_source_never_downloads(self):
        for value in [SimpleNamespace(status='READY',version='2',source=self.entry.source), SimpleNamespace(status='PENDING_REGISTRATION',version='1',source=self.entry.source), SimpleNamespace(status='READY',version='1',source='file:///untrusted')]:
            with patch('mlflow.MlflowClient') as client, patch('mlflow.artifacts.download_artifacts') as download:
                client.return_value.get_model_version.return_value=value
                with self.assertRaises(loader.ModelLoadError):loader.load_model('unused', self.startup)
                download.assert_not_called()

    def test_model_integrity_failure_does_not_retry_or_fallback(self):
        with patch('mlflow.MlflowClient') as client, patch('mlflow.artifacts.download_artifacts',return_value='/tmp/mock') as download, patch.object(loader,'_load_package',side_effect=ValueError(SECRET)) as package, patch.object(loader.joblib,'load') as fallback:
            client.return_value.get_model_version.return_value=self.entry
            with self.assertRaises(loader.ModelLoadError):loader.load_model('unused', self.startup)
            download.assert_called_once();package.assert_called_once();fallback.assert_not_called()
            self.assertFalse(self.clock.waits)

    def test_partial_download_retry_uses_fresh_directories(self):
        dirs=[]
        def download(**kwargs):
            dest=Path(kwargs['dst_path']);dirs.append(dest);(dest/'partial').write_text('synthetic')
            if len(dirs)==1:raise req.Timeout(SECRET)
            self.assertFalse(dirs[0].exists())
            return str(dest)
        with patch('mlflow.MlflowClient') as client, patch('mlflow.artifacts.download_artifacts',side_effect=download), patch.object(loader,'_load_package',return_value='safe'):
            client.return_value.get_model_version.return_value=self.entry
            self.assertEqual(loader.load_model('unused',self.startup),'safe')
        self.assertEqual(len(dirs),2);self.assertNotEqual(dirs[0],dirs[1]);self.assertTrue(all(not p.exists() for p in dirs))

    def test_late_download_is_cleaned_and_never_loaded(self):
        dirs=[]
        def download(**kwargs):
            dirs.append(Path(kwargs['dst_path']));self.clock.now=121;return str(dirs[-1])
        with patch('mlflow.MlflowClient') as client, patch('mlflow.artifacts.download_artifacts',side_effect=download), patch.object(loader,'_load_package') as load:
            client.return_value.get_model_version.return_value=self.entry
            with self.assertRaises(loader.ModelLoadError):loader.load_model('unused',self.startup)
            load.assert_not_called()
        self.assertTrue(all(not p.exists() for p in dirs))


class AppStartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(os.environ, {'DB_HOST':'fixture.invalid','DB_NAME':'test','DB_USER':'fixture','DB_PASSWORD':'synthetic','MODEL_SOURCE':'local'},clear=True), patch('joblib.load',return_value=MagicMock()):
            from backend import main
        cls.main=main

    def setUp(self):
        # Transport adapters are exercised with real SQLAlchemy above; these are app mocks.
        p=patch.object(self.main, 'configure_database_timeouts', side_effect=lambda engine, startup: engine)
        p.start();self.addCleanup(p.stop)
        p=patch.object(self.main.app.state,'startup_complete',False);p.start();self.addCleanup(p.stop)
        p=patch.object(self.main,'startup',Startup(clock=Clock(),sleep=lambda _:None));p.start();self.addCleanup(p.stop)

    def test_unready_http_requests_are_blocked_before_db_or_model(self):
        from fastapi.testclient import TestClient
        with patch.object(self.main,'engine') as engine,patch.object(self.main,'model') as model:
            client=TestClient(self.main.app)
            self.assertEqual(client.get('/ready').status_code,503)
            self.assertEqual(client.post('/predict',json={}).status_code,503)
            self.assertEqual(client.get('/history').status_code,503)
            self.assertEqual(client.get('/health').status_code,200)
            engine.connect.assert_not_called();engine.begin.assert_not_called();model.predict.assert_not_called()

    def test_probe_draft_cannot_succeed_before_initialization_or_without_model(self):
        import yaml
        from fastapi.testclient import TestClient
        draft = yaml.safe_load((Path(__file__).resolve().parents[1] /
            'docs/examples/fastapi-dev-startup-probe.patch.yaml').read_text())
        self.assertEqual(draft['metadata']['namespace'], 'datacenter-app-dev')
        path = draft['spec']['template']['spec']['containers'][0]['startupProbe']['httpGet']['path']
        self.assertEqual(path, '/startup')
        client = TestClient(self.main.app)  # intentionally no lifespan: guard must still hold
        with patch.object(self.main, 'engine') as engine:
            self.assertEqual(client.get(path).status_code, 503)
            self.assertEqual(client.get('/health').status_code, 200)
            self.main.app.state.startup_complete = True
            with patch.object(self.main, 'model', None):
                self.assertEqual(client.get(path).status_code, 503)
            engine.connect.assert_not_called()

    def test_startup_success_latches_without_runtime_db_or_age_dependency(self):
        from fastapi.testclient import TestClient
        clock = Clock()
        with patch.object(self.main, 'startup', Startup(clock=clock, sleep=clock.sleep)), patch.object(self.main, 'create_engine'), patch.object(self.main, 'model', object()), patch.object(self.main, 'engine') as engine:
            self.main.validate_database_schema()
            clock.now = 121  # budget ends after successful startup, not an application TTL
            engine.connect.side_effect = SQLAlchemyError(SECRET)
            client = TestClient(self.main.app)
            self.assertEqual(client.get('/startup').status_code, 200)
            self.assertEqual(client.get('/health').status_code, 200)
            engine.connect.assert_not_called()
            self.assertEqual(client.get('/ready').status_code, 503)
            engine.connect.assert_called_once()

    def test_exhausted_budget_blocks_startup_readiness_and_inference(self):
        from fastapi.testclient import TestClient
        self.main.startup.clock.now = 120
        with patch.object(self.main, 'create_engine') as create, patch.object(self.main, 'model') as model, patch.object(self.main, 'engine') as engine:
            with self.assertRaises(StartupError): self.main.validate_database_schema()
            client = TestClient(self.main.app)
            for path in ['/startup', '/ready']:
                self.assertEqual(client.get(path).status_code, 503)
            self.assertEqual(client.post('/predict', json={'server_id':'fixture','cpu':20,'memory':30,'temperature':40,'power':100}).status_code, 503)
            self.assertEqual(client.get('/health').status_code, 200)
            create.assert_not_called();engine.begin.assert_not_called();model.predict.assert_not_called()

    def test_successful_contract_enables_ready(self):
        with patch.object(self.main,'create_engine') as create,patch.object(self.main,'model',object()),patch.object(self.main,'engine') as engine:
            self.main.validate_database_schema()
            self.assertTrue(self.main.app.state.startup_complete)
            self.assertEqual(self.main.readiness(),{'status':'ready'})
            self.assertEqual(str(engine.connect.return_value.__enter__.return_value.execute.call_args.args[0]),'SELECT 1')
            create.return_value.begin.assert_not_called()

    def test_failed_schema_keeps_ready_false(self):
        with patch.object(self.main,'create_engine') as create:
            conn=create.return_value.connect.return_value.__enter__.return_value
            conn.execute.side_effect=ProgrammingError('',{},mysql.ProgrammingError(1146,SECRET))
            with self.assertRaises(StartupError):self.main.validate_database_schema()
            self.assertFalse(self.main.app.state.startup_complete)
            create.return_value.dispose.assert_called_once()

    def test_missing_model_keeps_ready_false(self):
        with patch.object(self.main,'create_engine'),patch.object(self.main,'model',None):
            with self.assertRaises(RuntimeError):self.main.validate_database_schema()
        self.assertFalse(self.main.app.state.startup_complete)

    def test_cleanup_failure_is_sanitized_and_blocks_ready(self):
        with patch.object(self.main, 'create_engine') as create:
            create.return_value.dispose.side_effect = RuntimeError(SECRET)
            with self.assertRaises(StartupError) as caught:
                self.main.validate_database_schema()
            self.assertNotIn(SECRET, str(caught.exception))
            self.assertFalse(self.main.app.state.startup_complete)

    def test_budget_expired_during_cleanup_blocks_ready(self):
        clock = Clock()
        with patch.object(self.main, 'startup', Startup(clock=clock, sleep=clock.sleep)), patch.object(self.main, 'create_engine') as create:
            create.return_value.dispose.side_effect = lambda: setattr(clock, 'now', 121)
            with self.assertRaises(StartupError):
                self.main.validate_database_schema()
            self.assertFalse(self.main.app.state.startup_complete)

    def test_ready_db_failure_does_not_break_process_health(self):
        self.main.app.state.startup_complete=True
        with patch.object(self.main,'engine') as engine:
            engine.connect.side_effect=SQLAlchemyError(SECRET)
            with self.assertRaises(self.main.HTTPException) as caught:self.main.readiness()
            self.assertEqual(caught.exception.status_code,503)
            self.assertEqual(self.main.health(),{'status':'ok'})
            engine.connect.assert_called_once()

    def test_normal_inference_retains_single_write_and_response(self):
        from fastapi.testclient import TestClient
        self.main.app.state.startup_complete=True
        with patch.object(self.main,'engine') as engine,patch.object(self.main,'model') as model:
            model.predict.return_value=[1];model.predict_proba.return_value=[[0.1,0.9]]
            conn=engine.begin.return_value.__enter__.return_value;conn.execute.return_value.lastrowid=777
            result=TestClient(self.main.app).post('/predict',json={'server_id':'fixture','cpu':20,'memory':30,'temperature':40,'power':100})
            self.assertEqual(result.status_code,200)
            self.assertEqual(result.json()['measurement_id'],777)
            self.assertEqual(result.json()['prediction'],'ANOMALY')
            self.assertEqual(result.json()['probability'],0.9)
            model.predict.assert_called_once();conn.execute.assert_called_once();engine.begin.assert_called_once()
            self.assertIn('INSERT INTO measurements',str(conn.execute.call_args.args[0]))

    def test_runtime_write_failure_is_never_retried(self):
        self.main.app.state.startup_complete=True
        with patch.object(self.main,'engine') as engine,patch.object(self.main,'model') as model:
            model.predict.return_value=[0];model.predict_proba.return_value=[[0.9,0.1]]
            engine.begin.side_effect=db_error(inner=TimeoutError(SECRET))
            request=self.main.PredictRequest(server_id='fixture',cpu=20,memory=30,temperature=40,power=100)
            with self.assertRaises(self.main.HTTPException):self.main.predict(request)
            engine.begin.assert_called_once()


if __name__=='__main__':unittest.main()
