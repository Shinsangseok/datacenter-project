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




# CD-06: these tests deliberately remain in the existing CI-collected module.
# Pure inference and request contracts use mocks; separate async tests below run
# the real Uvicorn server and kernel Unix socket with a locally trained RF fixture.
class CommonInferenceTests(unittest.TestCase):
    def test_wf01_feature_order_and_explicit_model_calls(self):
        from backend.inference import infer
        for label, prediction in [(0, 'NORMAL'), (1, 'ANOMALY')]:
            with self.subTest(label=label):
                model = Mock()
                model.predict.return_value = [label]
                model.predict_proba.return_value = [[0.1234567, 0.8765433]]
                result = infer(model, 20, 30, 40, 100)
                self.assertEqual(result.prediction, prediction)
                self.assertEqual(result.raw_probability, 0.8765433)
                model.predict.assert_called_once()
                model.predict_proba.assert_called_once()
                features = model.predict.call_args.args[0]
                self.assertIs(features, model.predict_proba.call_args.args[0])
                self.assertEqual(list(features.columns), ['cpu', 'memory', 'temperature', 'power'])
                self.assertEqual(features.values.tolist(), [[20, 30, 40, 100]])

    def test_wf02_raw_arrays_and_boundary_probabilities_preserved(self):
        from backend.inference import infer
        for probability in [0.0, 1.0, 0.123456789]:
            with self.subTest(probability=probability):
                labels, probabilities = [1], [[1 - probability, probability]]
                model = Mock(predict=Mock(return_value=labels), predict_proba=Mock(return_value=probabilities))
                result = infer(model, 1, 2, 3, 4)
                self.assertIs(result.raw_predictions, labels)
                self.assertIs(result.raw_probabilities, probabilities)
                self.assertEqual(result.raw_probability, probability)

    def test_wf03_helper_keeps_existing_nonbinary_label_semantics(self):
        from backend.inference import infer
        for label in [2, -1, 0.5]:
            model = Mock(predict=Mock(return_value=[label]), predict_proba=Mock(return_value=[[0.2, 0.8]]))
            self.assertEqual(infer(model, 1, 2, 3, 4).prediction, 'NORMAL')

    def test_wf04_predict_error_precedes_probability_call(self):
        from backend.inference import infer
        model = Mock(predict=Mock(side_effect=ValueError(SECRET)))
        with self.assertRaises(ValueError):
            infer(model, 1, 2, 3, 4)
        model.predict_proba.assert_not_called()

    def test_wf03_label_conversion_error_precedes_probability_call(self):
        from backend.inference import infer
        model = Mock(predict=Mock(return_value=['invalid-label']))
        with self.assertRaises(ValueError):
            infer(model, 1, 2, 3, 4)
        model.predict_proba.assert_not_called()

    def test_wf05_probability_error_preserves_call_order(self):
        from backend.inference import infer
        calls = []
        model = Mock()
        model.predict.side_effect = lambda _: calls.append('predict') or [1]
        def fail(_):
            calls.append('probability')
            raise ValueError(SECRET)
        model.predict_proba.side_effect = fail
        with self.assertRaises(ValueError):
            infer(model, 1, 2, 3, 4)
        self.assertEqual(calls, ['predict', 'probability'])

    def test_wf08_helper_has_no_application_or_io_dependencies(self):
        import ast
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'backend/inference.py').read_text())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or '').split('.')[0])
        self.assertFalse(names & {'main', 'backend', 'os', 'time', 'datetime', 'logging', 'requests', 'httpx', 'socket', 'sqlalchemy', 'pymysql', 'prometheus_client', 'mlflow'})

    def test_wf09_real_forest_is_deterministic_without_network(self):
        import pandas as pd
        from sklearn.ensemble import RandomForestClassifier
        from backend.inference import infer
        features = pd.DataFrame([[0, 0, 0, 0], [10, 10, 20, 100], [90, 90, 100, 900], [100, 100, 150, 1000]], columns=['cpu', 'memory', 'temperature', 'power'])
        forest = RandomForestClassifier(n_estimators=3, max_depth=2, random_state=7, n_jobs=1).fit(features, [0, 0, 1, 1])
        before = [tree.tree_.__getstate__()['nodes'].copy() for tree in forest.estimators_]
        with patch('socket.socket.connect', side_effect=AssertionError('external socket')), patch('requests.sessions.Session.request', side_effect=AssertionError('external HTTP')), patch('pymysql.connect', side_effect=AssertionError('DB')):
            first, second = infer(forest, 90, 90, 100, 900), infer(forest, 90, 90, 100, 900)
        self.assertEqual(first.prediction, 'ANOMALY')
        self.assertEqual(first.raw_probability, second.raw_probability)
        for original, tree in zip(before, forest.estimators_):
            self.assertTrue((original == tree.tree_.__getstate__()['nodes']).all())


class WriteFreePublicRegressionTests(unittest.TestCase):
    METRICS = ('prediction_counter', 'anomaly_counter', 'cpu_gauge', 'memory_gauge', 'temperature_gauge', 'power_gauge')
    PAYLOAD = {'server_id': 'fixture', 'cpu': 20, 'memory': 30, 'temperature': 40, 'power': 100}

    @classmethod
    def setUpClass(cls):
        AppStartupTests.setUpClass.__func__(cls)

    def setUp(self):
        from contextlib import ExitStack
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(self.main.app.state, 'startup_complete', True))
        self.engine = self.stack.enter_context(patch.object(self.main, 'engine'))
        self.model = self.stack.enter_context(patch.object(self.main, 'model'))
        self.model.predict.return_value = [1]
        self.model.predict_proba.return_value = [[0.1234567, 0.8765433]]
        self.conn = self.engine.begin.return_value.__enter__.return_value
        self.conn.execute.return_value.lastrowid = 777
        self.metrics = {name: self.stack.enter_context(patch.object(self.main, name)) for name in self.METRICS}
        from fastapi.testclient import TestClient
        self.client = TestClient(self.main.app, raise_server_exceptions=False)

    def assert_no_metrics(self):
        for metric in self.metrics.values():
            self.assertEqual(metric.mock_calls, [])

    def test_reg01_exact_response_and_raw_database_probability(self):
        for label, prediction in [(0, 'NORMAL'), (1, 'ANOMALY')]:
            with self.subTest(label=label):
                self.model.predict.return_value = [label]
                payload = {**self.PAYLOAD, 'measured_at': '2026-01-02T03:04:05+09:00'}
                response = self.client.post('/predict', json=payload)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {'measurement_id': 777, 'server_id': 'fixture', 'prediction': prediction, 'probability': 0.87654, 'measured_at': '2026-01-01T18:04:05Z'})
                bound = self.conn.execute.call_args.args[1]
                self.assertEqual(bound['probability'], 0.8765433)
                self.assertIsNone(bound['measured_at'].tzinfo)

    def test_reg01_naive_and_default_timestamp_contract(self):
        from datetime import datetime, timezone
        response = self.client.post('/predict', json={**self.PAYLOAD, 'measured_at': '2026-01-02T03:04:05'})
        self.assertEqual(response.json()['measured_at'], '2026-01-02T03:04:05Z')
        before = datetime.now(timezone.utc)
        response = self.client.post('/predict', json=self.PAYLOAD)
        after = datetime.now(timezone.utc)
        actual = datetime.fromisoformat(response.json()['measured_at'].replace('Z', '+00:00'))
        self.assertLessEqual(before, actual)
        self.assertLessEqual(actual, after)

    def test_reg02_single_insert_and_transaction_exit(self):
        self.assertEqual(self.client.post('/predict', json=self.PAYLOAD).status_code, 200)
        self.engine.begin.assert_called_once_with()
        self.conn.execute.assert_called_once()
        self.assertIn('INSERT INTO measurements', str(self.conn.execute.call_args.args[0]))
        self.engine.begin.return_value.__exit__.assert_called_once_with(None, None, None)
        self.engine.connect.assert_not_called()
        self.model.predict.assert_called_once()
        self.model.predict_proba.assert_called_once()

    def test_reg03_begin_execute_commit_failures_are_not_retried(self):
        for boundary in ['begin', 'execute', 'commit']:
            with self.subTest(boundary=boundary):
                self.engine.reset_mock()
                self.engine.begin.side_effect = None
                self.conn.execute.side_effect = None
                self.engine.begin.return_value.__exit__.side_effect = None
                if boundary == 'begin':
                    self.engine.begin.side_effect = SQLAlchemyError(SECRET)
                elif boundary == 'execute':
                    self.conn.execute.side_effect = SQLAlchemyError(SECRET)
                else:
                    self.engine.begin.return_value.__exit__.side_effect = SQLAlchemyError(SECRET)
                response = self.client.post('/predict', json=self.PAYLOAD)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json(), {'detail': 'could not save prediction'})
                self.assertNotIn(SECRET, response.text)
                self.engine.begin.assert_called_once()
                self.assert_no_metrics()

    def test_reg04_metrics_follow_commit_and_match_prediction(self):
        trace = []
        self.conn.execute.side_effect = lambda *args: trace.append('insert') or SimpleNamespace(lastrowid=777)
        self.engine.begin.return_value.__exit__.side_effect = lambda *args: trace.append('commit') or False
        for name, metric in self.metrics.items():
            metric.labels.side_effect = lambda _name=name, **kwargs: trace.append(_name + '.labels') or self.metrics[_name]
            metric.inc.side_effect = lambda _name=name: trace.append(_name + '.inc')
            metric.set.side_effect = lambda value, _name=name: trace.append(_name + '.set')
        response = self.client.post('/predict', json=self.PAYLOAD)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(trace, ['insert', 'commit', 'prediction_counter.labels', 'prediction_counter.inc', 'anomaly_counter.inc', 'cpu_gauge.labels', 'cpu_gauge.set', 'memory_gauge.labels', 'memory_gauge.set', 'temperature_gauge.labels', 'temperature_gauge.set', 'power_gauge.labels', 'power_gauge.set'])
        self.metrics['prediction_counter'].labels.assert_called_once_with(prediction='ANOMALY')
        self.metrics['anomaly_counter'].inc.assert_called_once_with()
        for name, field in [('cpu_gauge', 'cpu'), ('memory_gauge', 'memory'), ('temperature_gauge', 'temperature'), ('power_gauge', 'power')]:
            self.metrics[name].labels.assert_called_once_with(server_id='fixture')
            self.metrics[name].set.assert_called_once_with(self.PAYLOAD[field])

    def test_reg04_normal_does_not_increment_anomaly_counter(self):
        self.model.predict.return_value = [0]
        self.assertEqual(self.client.post('/predict', json=self.PAYLOAD).status_code, 200)
        self.metrics['prediction_counter'].labels.return_value.inc.assert_called_once_with()
        self.assertEqual(self.metrics['anomaly_counter'].mock_calls, [])

    def test_reg05_model_failures_keep_http500_and_do_not_write(self):
        for method in ['predict', 'predict_proba']:
            with self.subTest(method=method):
                self.model.reset_mock()
                self.model.predict.side_effect = None
                self.model.predict_proba.side_effect = None
                getattr(self.model, method).side_effect = RuntimeError(SECRET)
                response = self.client.post('/predict', json=self.PAYLOAD)
                self.assertEqual(response.status_code, 500)
                self.assertNotIn(SECRET, response.text)
                self.engine.begin.assert_not_called()
                self.assert_no_metrics()
                if method == 'predict':
                    self.model.predict_proba.assert_not_called()

    def test_reg05_existing_input_coercion_and_extra_rejection(self):
        response = self.client.post('/predict', json={**self.PAYLOAD, 'cpu': '20.5'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.conn.execute.call_args.args[1]['cpu'], 20.5)
        self.model.reset_mock(); self.engine.reset_mock()
        response = self.client.post('/predict', json={**self.PAYLOAD, 'dry_run': True})
        self.assertEqual(response.status_code, 422)
        self.engine.begin.assert_not_called(); self.model.predict.assert_not_called()

    def test_reg05_nonbinary_model_label_remains_normal_on_public_api(self):
        self.model.predict.return_value = [2]
        response = self.client.post('/predict', json=self.PAYLOAD)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['prediction'], 'NORMAL')
        self.engine.begin.assert_called_once()

    def test_uds01_public_routes_and_openapi_do_not_expose_verifier(self):
        from backend.write_free_verifier import VERIFICATION_PATH
        inventory = {route.path for route in self.main.app.routes}
        self.assertNotIn(VERIFICATION_PATH, inventory)
        self.assertNotIn(VERIFICATION_PATH, self.client.get('/openapi.json').json()['paths'])
        for method in ['GET', 'POST', 'HEAD', 'OPTIONS']:
            for suffix in ['', '/', '?dry_run=true']:
                response = self.client.request(method, VERIFICATION_PATH + suffix, headers={'X-Forwarded-For': '127.0.0.1', 'Forwarded': 'for=127.0.0.1', 'Host': 'localhost'})
                self.assertEqual(response.status_code, 404)
        self.model.predict.assert_not_called(); self.engine.begin.assert_not_called()
        self.assert_no_metrics()

    def test_reg09_new_tests_are_in_existing_ci_explicit_module(self):
        workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/fastapi-ci.yml').read_text()
        self.assertIn('\"test_fastapi_startup\"', workflow)
        self.assertEqual(type(self).__module__, __name__)



class WriteFreeActivationTests(unittest.TestCase):
    ENV = {
        'WRITE_FREE_INFERENCE_ENABLED': 'true',
        'POD_NAMESPACE': 'datacenter-app-dev',
        'MLFLOW_ENV': 'dev',
        'MODEL_SOURCE': 'registry',
        'MLFLOW_TRACKING_URI': 'http://mlflow.mlflow-dev.svc.cluster.local:5000',
    }

    def test_wf14_disabled_by_default_and_false(self):
        from backend.write_free_verifier import WriteFreeVerifier
        for env in [{}, {'WRITE_FREE_INFERENCE_ENABLED': 'false'}]:
            with self.subTest(env=env), patch('socket.socket') as sock:
                verifier = WriteFreeVerifier(environ=env)
                self.assertFalse(verifier.enabled)
                sock.assert_not_called()

    def test_wf14_unknown_flags_are_configuration_errors(self):
        from backend.write_free_verifier import WriteFreeVerifier, VerifierError
        for value in ['TRUE', '1', 'yes', '', ' true', 'true ']:
            with self.subTest(value=value), self.assertRaises(VerifierError):
                WriteFreeVerifier(environ={**self.ENV, 'WRITE_FREE_INFERENCE_ENABLED': value})

    def test_wf15_prod_implicit_prod_and_unknown_environment_fail_closed(self):
        from backend.write_free_verifier import WriteFreeVerifier, VerifierError
        for value in ['prod', 'production', '', None]:
            env = dict(self.ENV)
            if value is None:
                env.pop('MLFLOW_ENV')
            else:
                env['MLFLOW_ENV'] = value
            with self.subTest(value=value), self.assertRaises(VerifierError):
                WriteFreeVerifier(environ=env)

    def test_wf16_enable_and_dev_flags_are_insufficient(self):
        from backend.write_free_verifier import WriteFreeVerifier, VerifierError
        with self.assertRaises(VerifierError):
            WriteFreeVerifier(environ={'WRITE_FREE_INFERENCE_ENABLED': 'true', 'MLFLOW_ENV': 'dev'})

    def test_uds08_namespace_registry_source_and_target_are_all_required(self):
        from backend.write_free_verifier import WriteFreeVerifier, VerifierError
        mutations = [('POD_NAMESPACE', 'datacenter-app'), ('POD_NAMESPACE', ''), ('MODEL_SOURCE', 'local'), ('MODEL_SOURCE', 'unknown')]
        for key, value in mutations:
            with self.subTest(key=key, value=value), self.assertRaises(VerifierError):
                WriteFreeVerifier(environ={**self.ENV, key: value})
        self.assertTrue(WriteFreeVerifier(environ=dict(self.ENV)).enabled)

    def test_uds11_multiworker_configuration_is_rejected(self):
        from backend.write_free_verifier import WriteFreeVerifier, VerifierError
        for key in ['WEB_CONCURRENCY', 'UVICORN_WORKERS']:
            for value in ['2', '0', '-1', 'garbage']:
                with self.subTest(key=key, value=value), self.assertRaises(VerifierError):
                    WriteFreeVerifier(environ={**self.ENV, key: value})


class WriteFreeUDSTests(unittest.IsolatedAsyncioTestCase):
    """Real Uvicorn/UDS lifecycle, using this test process's UID in a private tmpdir.

    The runtime UID=10001 gate is checked independently; CI need not be root to
    exercise the real kernel socket. A privileged cross-UID integration proof is
    deliberately distinct from these portable tests.
    """
    PAYLOAD = {'cpu': 90, 'memory': 90, 'temperature': 100, 'power': 900}

    async def asyncSetUp(self):
        import asyncio
        from contextlib import ExitStack
        import tempfile
        import pandas as pd
        from sklearn.ensemble import RandomForestClassifier
        from backend import write_free_verifier as verifier_module
        self.vmod = verifier_module
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='cd06-uds-')))
        self.socket_dir = self.directory / 'private'
        self.socket_path = self.socket_dir / 'inference.sock'
        self.stack.enter_context(patch.object(self.vmod, 'SOCKET_DIRECTORY', self.socket_dir))
        self.stack.enter_context(patch.object(self.vmod, 'SOCKET_PATH', self.socket_path))
        self.stack.enter_context(patch.object(self.vmod, 'EXPECTED_UID', os.geteuid()))
        frame = pd.DataFrame([[0, 0, 0, 0], [10, 10, 20, 100], [90, 90, 100, 900], [100, 100, 150, 1000]], columns=['cpu', 'memory', 'temperature', 'power'])
        self.model = RandomForestClassifier(n_estimators=3, max_depth=2, random_state=7, n_jobs=1).fit(frame, [0, 0, 1, 1])
        self.ready = True
        self.current_model = self.model
        self.verifier = self.vmod.WriteFreeVerifier(environ=dict(WriteFreeActivationTests.ENV))
        self.started = False

    async def asyncTearDown(self):
        await self.verifier.stop()

    async def start(self):
        await self.verifier.start(lambda: self.current_model, lambda: self.ready)
        self.started = True
        self.assertTrue(self.verifier.running)

    async def request(self, payload=None, *, method='POST', path='/verify/predict', **kwargs):
        import httpx
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.verifier.app), base_url='http://uds') as client:
            if payload is None and method == 'POST' and not kwargs:
                payload = self.PAYLOAD
            if payload is not None:
                kwargs['json'] = payload
            return await client.request(method, path, **kwargs)

    async def raw_asgi(self, *, chunks=None, headers=None, method='POST', path='/verify/predict', receive=None):
        """Feed arbitrary ASGI body fragments; HTTP framing is tested over UDS separately."""
        import asyncio
        chunks = list(chunks if chunks is not None else [json.dumps(self.PAYLOAD).encode()])
        headers = list(headers if headers is not None else [(b'content-type', b'application/json')])
        messages = []
        index = 0
        async def default_receive():
            nonlocal index
            if index < len(chunks):
                chunk = chunks[index]
                index += 1
                return {'type': 'http.request', 'body': chunk, 'more_body': index < len(chunks)}
            await asyncio.sleep(0)
            return {'type': 'http.disconnect'}
        async def send(message):
            messages.append(message)
        await self.verifier.app({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'scheme': 'http', 'method': method, 'path': path, 'raw_path': path.encode(), 'query_string': b'', 'root_path': '', 'headers': headers, 'client': None, 'server': None}, receive or default_receive, send)
        status = next(message['status'] for message in messages if message['type'] == 'http.response.start')
        body = b''.join(message.get('body', b'') for message in messages if message['type'] == 'http.response.body')
        return status, body

    async def uds_http(self, *, body=None, method='POST', path='/verify/predict', chunked=False):
        import asyncio
        if body is None:
            body = json.dumps(self.PAYLOAD).encode()
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        if chunked:
            headers = 'Transfer-Encoding: chunked\r\n'
            encoded = b''.join(hex(len(part))[2:].encode() + b'\r\n' + part + b'\r\n' for part in [body[:7], body[7:]]) + b'0\r\n\r\n'
        else:
            headers = f'Content-Length: {len(body)}\r\n'
            encoded = body
        writer.write((f'{method} {path} HTTP/1.1\r\nHost: uds\r\nContent-Type: application/json\r\nConnection: close\r\n' + headers + '\r\n').encode() + encoded)
        await writer.drain()
        try:
            response = await asyncio.wait_for(reader.read(), timeout=3)
        finally:
            writer.close()
            await writer.wait_closed()
        head, _, content = response.partition(b'\r\n\r\n')
        return int(head.split(b' ', 2)[1]), content

    async def test_wf09_real_http_uds_prediction_and_probability(self):
        from backend.inference import infer
        await self.start()
        expected = infer(self.model, **self.PAYLOAD)
        status, body = await self.uds_http()
        self.assertEqual(status, 200)
        response = json.loads(body)
        self.assertEqual(response, {'mode': 'write_free', 'prediction': expected.prediction, 'probability': round(expected.raw_probability, 5), 'persisted': False})
        self.assertNotIn('measurement_id', response)
        self.assertNotIn('measured_at', response)

    async def test_uds02_prebound_unix_socket_has_no_tcp_bind(self):
        bound_families = []
        original_bind = socket.socket.bind
        def record_bind(sock, address):
            bound_families.append(sock.family)
            return original_bind(sock, address)
        with patch('socket.socket.bind', new=record_bind):
            await self.start()
            status, _ = await self.uds_http()
        self.assertEqual(status, 200)
        self.assertEqual(bound_families, [socket.AF_UNIX])

    async def test_uds03_permissions_and_owner_survive_server_start(self):
        import stat
        await self.start()
        for path, mode in [(self.socket_dir, 0o700), (self.socket_path, 0o600)]:
            metadata = path.lstat()
            self.assertEqual(stat.S_IMODE(metadata.st_mode), mode)
            self.assertEqual(metadata.st_uid, os.geteuid())
        self.assertTrue(stat.S_ISSOCK(self.socket_path.lstat().st_mode))
        self.assertEqual((await self.uds_http())[0], 200)
        self.assertEqual(stat.S_IMODE(self.socket_path.stat().st_mode), 0o600)

    async def test_uds03_wrong_runtime_uid_is_rejected(self):
        with patch.object(self.vmod, 'EXPECTED_UID', os.geteuid() + 1):
            with self.assertRaises(self.vmod.VerifierError):
                await self.start()
        self.assertFalse(self.socket_path.exists())

    async def test_uds04_symlink_directory_is_not_followed_or_modified(self):
        real_directory = self.directory / 'target'
        real_directory.mkdir(mode=0o755)
        # Other offline suites may deliberately set umask 077. Establish the
        # insecure fixture explicitly; the verifier must never repair it.
        real_directory.chmod(0o755)
        self.socket_dir.symlink_to(real_directory, target_is_directory=True)
        with self.assertRaises(self.vmod.VerifierError):
            await self.start()
        self.assertTrue(self.socket_dir.is_symlink())
        self.assertFalse((real_directory / 'inference.sock').exists())
        self.assertEqual(real_directory.stat().st_mode & 0o777, 0o755)

    async def test_uds04_existing_socket_file_is_never_overwritten(self):
        self.socket_dir.mkdir(mode=0o700)
        self.socket_path.write_bytes(b'preserve-foreign-file')
        with self.assertRaises(self.vmod.VerifierError):
            await self.start()
        self.assertEqual(self.socket_path.read_bytes(), b'preserve-foreign-file')

    async def test_uds04_existing_socket_symlink_is_not_followed(self):
        self.socket_dir.mkdir(mode=0o700)
        target = self.directory / 'target-file'
        target.write_bytes(b'untouched')
        self.socket_path.symlink_to(target)
        with self.assertRaises(self.vmod.VerifierError):
            await self.start()
        self.assertTrue(self.socket_path.is_symlink())
        self.assertEqual(target.read_bytes(), b'untouched')

    async def test_uds04_wrong_directory_mode_is_not_repaired(self):
        self.socket_dir.mkdir(mode=0o755)
        # mkdir applies the process umask; this test requires an actual 0755
        # directory even when a preceding Auth test left umask 077.
        self.socket_dir.chmod(0o755)
        with self.assertRaises(self.vmod.VerifierError):
            await self.start()
        self.assertEqual(self.socket_dir.stat().st_mode & 0o777, 0o755)
        self.assertFalse(self.socket_path.exists())

    async def test_uds05_not_ready_never_binds_or_serves(self):
        self.ready = False
        with self.assertRaises(self.vmod.VerifierError):
            await self.start()
        self.assertFalse(self.socket_path.exists())
        self.assertEqual((await self.request()).status_code, 503)

    async def test_uds05_shutdown_removes_owned_socket_only(self):
        await self.start()
        await self.verifier.stop()
        self.assertFalse(self.verifier.running)
        self.assertFalse(self.socket_path.exists())
        self.assertEqual((await self.request()).status_code, 503)
        await self.verifier.stop()  # repeated shutdown must be harmless

    async def test_uds05_shutdown_does_not_unlink_replacement_file(self):
        await self.start()
        self.socket_path.unlink()
        self.socket_path.write_bytes(b'not-owned-by-server')
        await self.verifier.stop()
        self.assertEqual(self.socket_path.read_bytes(), b'not-owned-by-server')

    async def test_uds11_signal_handler_preserved_and_duplicate_owner_rejected(self):
        import signal
        before = {sig: signal.getsignal(sig) for sig in [signal.SIGINT, signal.SIGTERM]}
        await self.start()
        self.assertEqual({sig: signal.getsignal(sig) for sig in before}, before)
        other = self.vmod.WriteFreeVerifier(environ=dict(WriteFreeActivationTests.ENV))
        try:
            with self.assertRaises(self.vmod.VerifierError):
                await other.start(lambda: self.model, lambda: True)
        finally:
            await other.stop()
        self.assertTrue(self.socket_path.exists())
        self.assertEqual((await self.uds_http())[0], 200)
        await self.verifier.stop()
        self.assertEqual({sig: signal.getsignal(sig) for sig in before}, before)

    async def test_wf10_missing_model_and_changed_identity_fail_closed(self):
        await self.start()
        for value in [None, object()]:
            self.current_model = value
            response = await self.request()
            self.assertEqual(response.status_code, 503)
            self.assertNotIn(SECRET, response.text)

    async def test_wf10_missing_model_at_startup_never_binds(self):
        self.current_model = None
        with self.assertRaises(self.vmod.VerifierError):
            await self.start()
        self.assertFalse(self.socket_path.exists())

    async def test_uds07_nonforest_subclass_features_and_classes_fail_closed(self):
        from sklearn.ensemble import RandomForestClassifier
        import copy
        class DerivedForest(RandomForestClassifier):
            pass
        invalid_models = [Mock(), DerivedForest()]
        wrong_features = copy.deepcopy(self.model)
        wrong_features.feature_names_in_ = ['memory', 'cpu', 'temperature', 'power']
        invalid_models.append(wrong_features)
        wrong_classes = copy.deepcopy(self.model)
        wrong_classes.classes_ = [1, 0]
        invalid_models.append(wrong_classes)
        for invalid in invalid_models:
            self.verifier = self.vmod.WriteFreeVerifier(environ=dict(WriteFreeActivationTests.ENV))
            self.current_model = invalid
            with self.subTest(kind=type(invalid).__name__), self.assertRaises(self.vmod.VerifierError):
                await self.start()
            self.assertFalse(self.socket_path.exists())

    async def test_wf12_readiness_loss_blocks_model_before_computation(self):
        await self.start()
        self.ready = False
        with patch.object(self.model, 'predict') as predict:
            self.assertEqual((await self.request()).status_code, 503)
            predict.assert_not_called()

    async def test_wf03_invalid_raw_outputs_are_sanitized(self):
        import numpy as np
        cases = [([2], [[0.2, 0.8]]), ([0.5], [[0.2, 0.8]]), ([float('nan')], [[0.2, 0.8]]), ([1, 0], [[0.2, 0.8]]), ([], []), ([1], [[0.2]]), ([1], [[0.1, float('nan')]]), ([1], [[float('inf'), 0.8]]), ([1], [[-0.1, 1.1]]), ([1], [[0.4, 0.8]]), ([1], [[0.2, 0.8], [0.7, 0.3]]), ([1], [[0.2, 0.8], [0.2]])]
        await self.start()
        # New instance per group preserves the ten-request production budget.
        for index, (labels, probabilities) in enumerate(cases):
            if index == 8:
                await self.verifier.stop()
                self.verifier = self.vmod.WriteFreeVerifier(environ=dict(WriteFreeActivationTests.ENV))
                await self.start()
            with self.subTest(index=index), patch.object(self.model, 'predict', return_value=labels), patch.object(self.model, 'predict_proba', return_value=probabilities):
                response = await self.request()
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json(), {'detail': 'verification unavailable'})

    async def test_wf04_wf05_failures_do_not_disclose_upstream_error(self):
        await self.start()
        for method in ['predict', 'predict_proba']:
            with self.subTest(method=method), patch.object(self.model, method, side_effect=RuntimeError(SECRET)):
                response = await self.request()
                self.assertEqual(response.status_code, 503)
                self.assertNotIn(SECRET, response.text)
                self.assertNotIn('RuntimeError', response.text)

    async def test_wf06_wf07_wf08_request_has_no_db_external_io_or_business_metrics(self):
        from contextlib import ExitStack
        AppStartupTests.setUpClass.__func__(WriteFreePublicRegressionTests)
        main = WriteFreePublicRegressionTests.main
        await self.start()
        original_connect = socket.socket.connect
        def unix_only(sock, address):
            if sock.family != socket.AF_UNIX:
                raise AssertionError('forbidden external socket')
            return original_connect(sock, address)
        with ExitStack() as traps:
            engine = traps.enter_context(patch.object(main, 'engine'))
            for method in ['begin', 'connect', 'execute']:
                getattr(engine, method).side_effect = AssertionError('request DB call')
            metrics = [traps.enter_context(patch.object(main, name)) for name in WriteFreePublicRegressionTests.METRICS]
            for metric in metrics:
                for method in ['labels', 'inc', 'set']:
                    getattr(metric, method).side_effect = AssertionError('business metric changed')
            calls = [traps.enter_context(patch(target, side_effect=AssertionError('forbidden request I/O'))) for target in ['pymysql.connect', 'pymysql.connections.Connection.connect', 'requests.sessions.Session.request', 'httpx.HTTPTransport.handle_request', 'mlflow.MlflowClient', 'mlflow.artifacts.download_artifacts', 'mlflow.log_model' if hasattr(__import__('mlflow'), 'log_model') else 'mlflow.sklearn.log_model', 'backend.model_loader.load_model', 'backend.dify.run_analysis']]
            traps.enter_context(patch('socket.socket.connect', new=unix_only))
            for payload in [self.PAYLOAD, {**self.PAYLOAD, 'cpu': 0, 'memory': 0, 'temperature': 0, 'power': 0}]:
                self.assertEqual((await self.request(payload)).status_code, 200)
            with patch.object(self.model, 'predict', side_effect=RuntimeError(SECRET)):
                self.assertEqual((await self.request()).status_code, 503)
            self.ready = False
            self.assertEqual((await self.request()).status_code, 503)
            self.assertEqual(engine.mock_calls, [])
            for metric in metrics:
                self.assertEqual(metric.mock_calls, [])
            for call in calls:
                call.assert_not_called()

    async def test_wf21_feature_only_strict_finite_schema(self):
        await self.start()
        invalid = [{'server_id': 'fixture'}, {'measured_at': '2026-01-01'}, {'unknown': 1}, {'cpu': '20'}, {'cpu': True}, {'cpu': -1}, {'cpu': 101}, {'memory': 101}, {'temperature': -51}, {'temperature': 151}, {'power': -1}, {'power': 10001}]
        for index, change in enumerate(invalid):
            if index == 8:
                await self.verifier.stop()
                self.verifier = self.vmod.WriteFreeVerifier(environ=dict(WriteFreeActivationTests.ENV))
                await self.start()
            with self.subTest(change=change), patch.object(self.model, 'predict') as predict:
                response = await self.request({**self.PAYLOAD, **change})
                self.assertEqual(response.status_code, 422)
                predict.assert_not_called()
                self.assertNotIn('input', response.text)

    async def test_wf21_nan_infinity_and_missing_features_rejected(self):
        await self.start()
        bodies = [b'{"cpu":NaN,"memory":1,"temperature":1,"power":1}', b'{"cpu":Infinity,"memory":1,"temperature":1,"power":1}', b'{"cpu":1,"memory":1,"temperature":1}', b'{invalid json}']
        with patch.object(self.model, 'predict') as predict:
            for body in bodies:
                status, response = await self.raw_asgi(chunks=[body])
                self.assertEqual(status, 422)
                self.assertNotIn(b'input', response)
            predict.assert_not_called()

    async def test_wf22_uds09_only_post_route_no_docs_redirect_or_cors(self):
        await self.start()
        self.assertEqual([(route.path, route.methods) for route in self.verifier.app.routes], [('/verify/predict', {'POST'})])
        for method, path, expected in [('GET', '/verify/predict', 405), ('HEAD', '/verify/predict', 405), ('OPTIONS', '/verify/predict', 405), ('POST', '/verify/predict/', 404), ('GET', '/docs', 404), ('GET', '/redoc', 404), ('GET', '/openapi.json', 404), ('GET', '/docs/oauth2-redirect', 404)]:
            response = await self.request(method=method, path=path, headers={'Origin': 'https://fixture.invalid', 'Authorization': 'Bearer ' + SECRET})
            self.assertEqual(response.status_code, expected)
            self.assertNotIn('access-control-allow-origin', response.headers)
            self.assertNotIn(SECRET, response.text)

    async def test_wf23_every_http_request_counts_before_routing_or_validation(self):
        await self.start()
        for method, path, kwargs in [('GET', '/unknown', {}), ('GET', '/verify/predict', {}), ('POST', '/verify/predict', {'payload': {}}), ('POST', '/verify/predict', {'content': b'x' * 4097})]:
            await self.request(method=method, path=path, **kwargs)
        self.assertEqual(self.verifier.requests_seen, 4)
        for _ in range(6):
            await self.request(method='GET', path='/unknown')
        self.assertEqual(self.verifier.requests_seen, 10)
        with patch.object(self.model, 'predict') as predict:
            self.assertEqual((await self.request()).status_code, 429)
            predict.assert_not_called()

    async def test_wf23_body_limit_uses_actual_bytes_not_content_length(self):
        await self.start()
        good = json.dumps(self.PAYLOAD).encode()
        exact = good + b' ' * (4096 - len(good))
        for headers in [[], [(b'content-length', b'1')], [(b'transfer-encoding', b'chunked')]]:
            status, _ = await self.raw_asgi(chunks=[exact[:17], exact[17:]], headers=[(b'content-type', b'application/json'), *headers])
            self.assertEqual(status, 200)
            with patch.object(self.model, 'predict') as predict:
                status, _ = await self.raw_asgi(chunks=[exact, b' '], headers=[(b'content-type', b'application/json'), *headers])
                self.assertEqual(status, 413)
                predict.assert_not_called()

    async def test_wf23_real_http_chunked_body_limit(self):
        await self.start()
        self.assertEqual((await self.uds_http(chunked=True))[0], 200)
        self.assertEqual((await self.uds_http(body=b'x' * 4097, chunked=True))[0], 413)

    async def test_wf23_slow_body_has_separate_deadline(self):
        import asyncio
        await self.start()
        async def receive():
            await asyncio.sleep(1)
            return {'type': 'http.request', 'body': b'', 'more_body': True}
        with patch.object(self.vmod, 'BODY_TIMEOUT_SECONDS', 0.02), patch.object(self.model, 'predict') as predict:
            status, _ = await self.raw_asgi(receive=receive)
            self.assertEqual(status, 408)
            predict.assert_not_called()

    async def test_wf20_ttl_uses_monotonic_and_does_not_restart_main(self):
        import time
        clock = Clock()
        with patch.object(self.verifier, '_clock', clock):
            await self.start()
            clock.now = 599
            self.assertEqual((await self.request()).status_code, 200)
            with patch('time.time', return_value=-9999999):
                self.assertEqual((await self.request()).status_code, 200)
            clock.now = 600
            with patch.object(self.model, 'predict') as predict:
                self.assertEqual((await self.request()).status_code, 429)
                predict.assert_not_called()
            self.assertTrue(self.verifier.running)

    async def test_uds10_cancelled_request_keeps_slot_until_native_work_finishes(self):
        import asyncio
        import threading
        await self.start()
        entered, release = threading.Event(), threading.Event()
        original_predict = self.model.predict
        def slow_predict(features):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('offline fixture deadline')
            return original_predict(features)
        with patch.object(self.model, 'predict', side_effect=slow_predict):
            request = asyncio.create_task(self.request())
            try:
                for _ in range(100):
                    if entered.is_set():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(entered.is_set())
                request.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await request
                self.assertTrue(self.verifier.inference_active)
                self.assertEqual((await self.request()).status_code, 429)
            finally:
                release.set()
            for _ in range(100):
                if not self.verifier.inference_active:
                    break
                await asyncio.sleep(0.01)
            self.assertFalse(self.verifier.inference_active)
        self.assertEqual((await self.request()).status_code, 200)

    async def test_uds10_real_http_timeout_does_not_free_busy_slot(self):
        import asyncio
        import threading
        await self.start()
        entered, release = threading.Event(), threading.Event()
        original_predict = self.model.predict
        def slow_predict(features):
            entered.set()
            release.wait(3)
            return original_predict(features)
        with patch.object(self.model, 'predict', side_effect=slow_predict):
            request = asyncio.create_task(self.uds_http())
            try:
                for _ in range(100):
                    if entered.is_set():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(entered.is_set())
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(request, timeout=0.03)
                self.assertTrue(self.verifier.inference_active)
                status, _ = await self.uds_http()
                self.assertEqual(status, 429)
            finally:
                release.set()
            for _ in range(100):
                if not self.verifier.inference_active:
                    break
                await asyncio.sleep(0.01)
        self.assertEqual((await self.uds_http())[0], 200)

    async def test_wf23_concurrent_eleven_requests_cannot_exceed_budget(self):
        import asyncio
        await self.start()
        replies = await asyncio.gather(*(self.request(method='GET', path='/unknown') for _ in range(11)))
        self.assertEqual(sum(reply.status_code == 404 for reply in replies), 10)
        self.assertEqual(sum(reply.status_code == 429 for reply in replies), 1)
        self.assertEqual(self.verifier.requests_seen, 11)

    async def test_uds06_uds12_shared_model_identity_and_state_are_unchanged(self):
        import pickle
        await self.start()
        before = pickle.dumps(self.model)
        with patch('backend.model_loader.load_model', side_effect=AssertionError('reload')), patch('mlflow.artifacts.download_artifacts', side_effect=AssertionError('download')), patch.object(type(self.model), 'fit', side_effect=AssertionError('fit')):
            self.assertEqual((await self.request()).status_code, 200)
        self.assertIs(self.current_model, self.model)
        self.assertEqual(pickle.dumps(self.model), before)



    async def test_wf08_request_does_not_open_files_for_writing(self):
        import builtins
        import io
        await self.start()
        operations = []
        original_builtin, original_io, original_os = builtins.open, io.open, os.open
        def checked_open(original):
            def check(file, mode='r', *args, **kwargs):
                if any(flag in mode for flag in 'wax+'):
                    operations.append('file-write')
                    raise AssertionError('request attempted filesystem write')
                return original(file, mode, *args, **kwargs)
            return check
        def checked_os_open(path, flags, *args, **kwargs):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                operations.append('os-write')
                raise AssertionError('request attempted filesystem write')
            return original_os(path, flags, *args, **kwargs)
        with patch('builtins.open', new=checked_open(original_builtin)), patch('io.open', new=checked_open(original_io)), patch('os.open', new=checked_os_open):
            self.assertEqual((await self.request()).status_code, 200)
        self.assertEqual(operations, [])

    async def test_wf21_huge_json_integer_is_sanitized_validation_error(self):
        await self.start()
        with patch.object(self.model, 'predict') as predict:
            response = await self.request({**self.PAYLOAD, 'cpu': 10 ** 400})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), {'detail': 'invalid verifier request'})
        predict.assert_not_called()

    async def test_wf20_body_receipt_cannot_extend_monotonic_lease(self):
        clock = Clock()
        with patch.object(self.verifier, '_clock', clock):
            await self.start()
            async def receive():
                clock.now = 600
                return {'type': 'http.request', 'body': json.dumps(self.PAYLOAD).encode(), 'more_body': False}
            with patch.object(self.model, 'predict') as predict:
                status, _ = await self.raw_asgi(receive=receive)
                self.assertEqual(status, 429)
                predict.assert_not_called()

    async def test_reg10_payload_credentials_and_model_failures_are_not_logged(self):
        from contextlib import ExitStack
        records = []
        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
        capture = Capture()
        loggers = [logging.getLogger(), logging.getLogger('uvicorn.error'), logging.getLogger('uvicorn.access')]
        for logger in loggers:
            logger.addHandler(capture)
        try:
            await self.start()
            status, body = await self.uds_http(body=json.dumps({**self.PAYLOAD, 'secret': SECRET}).encode())
            self.assertEqual(status, 422)
            self.assertNotIn(SECRET.encode(), body)
            with patch.object(self.model, 'predict', side_effect=RuntimeError(SECRET)):
                response = await self.request(self.PAYLOAD, headers={'Authorization': 'Bearer ' + SECRET})
            self.assertEqual(response.status_code, 503)
            self.assertNotIn(SECRET, response.text)
            self.assertFalse(any(SECRET in record for record in records))
        finally:
            for logger in loggers:
                logger.removeHandler(capture)

    async def test_uds05_shutdown_drains_native_work_before_owned_socket_cleanup(self):
        import asyncio
        import threading
        await self.start()
        entered, release = threading.Event(), threading.Event()
        original_predict = self.model.predict
        def slow_predict(features):
            entered.set()
            release.wait(3)
            return original_predict(features)
        with patch.object(self.model, 'predict', side_effect=slow_predict):
            request = asyncio.create_task(self.request())
            shutdown = None
            try:
                for _ in range(100):
                    if entered.is_set():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(entered.is_set())
                shutdown = asyncio.create_task(self.verifier.stop())
                await asyncio.sleep(0.03)
                self.assertFalse(shutdown.done())
                self.assertFalse(self.verifier.running)
                self.assertTrue(self.verifier.inference_active)
                self.assertTrue(self.socket_path.exists())
            finally:
                release.set()
                await request
                if shutdown is not None:
                    await shutdown
        self.assertFalse(self.socket_path.exists())


    async def test_reg10_real_uds_trace_hides_authorization_query_and_preserves_loggers(self):
        import asyncio
        from uvicorn.logging import TRACE_LOG_LEVEL
        records = []
        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
        capture = Capture(level=TRACE_LOG_LEVEL)
        loggers = [logging.getLogger(name) for name in ('uvicorn.error', 'uvicorn.access', 'uvicorn.asgi')]
        absent = object()
        original = {logger: (logger.level, list(logger.handlers), logger.propagate, logger.disabled, getattr(logger, 'trace', absent)) for logger in loggers}
        original_disable = logging.root.manager.disable
        writer = None
        try:
            logging.disable(logging.NOTSET)
            for logger in loggers:
                logger.setLevel(TRACE_LOG_LEVEL)
                logger.handlers[:] = [capture]
                logger.propagate = False
                logger.disabled = False
            configured = {logger: (logger.level, list(logger.handlers), logger.propagate) for logger in loggers}
            await self.start()
            self.assertEqual({logger: (logger.level, list(logger.handlers), logger.propagate) for logger in loggers}, configured)
            header_secret = 'cd06-header-secret-never-log'
            query_secret = 'cd06-query-secret-never-log'
            payload = json.dumps(self.PAYLOAD).encode()
            reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
            request = (f'POST /verify/predict?verification={query_secret} HTTP/1.1\r\n'
                       f'Host: uds\r\nAuthorization: Bearer {header_secret}\r\n'
                       f'Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n'
                       'Connection: close\r\n\r\n').encode() + payload
            writer.write(request)
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), timeout=3)
            self.assertTrue(response.startswith(b'HTTP/1.1 200 '))
            body = json.loads(response.partition(b'\r\n\r\n')[2])
            self.assertFalse(body['persisted'])
            writer.close()
            await writer.wait_closed()
            writer = None
            await self.verifier.stop()
            self.assertEqual({logger: (logger.level, list(logger.handlers), logger.propagate) for logger in loggers}, configured)
            self.assertFalse(any(header_secret in record or query_secret in record for record in records))
            self.assertFalse(any('Started scope=' in record for record in records))
        finally:
            try:
                if writer is not None:
                    writer.close()
                    await writer.wait_closed()
                await self.verifier.stop()
            finally:
                for logger, (level, handlers, propagate, disabled, trace) in original.items():
                    logger.setLevel(level)
                    logger.handlers[:] = handlers
                    logger.propagate = propagate
                    logger.disabled = disabled
                    if trace is absent:
                        if hasattr(logger, 'trace'):
                            delattr(logger, 'trace')
                    else:
                        logger.trace = trace
                logging.disable(original_disable)


class WriteFreeMainLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """Execute the real primary FastAPI lifespan with DB/model loading isolated."""

    async def asyncSetUp(self):
        from contextlib import ExitStack
        await WriteFreeUDSTests.asyncSetUp(self)
        AppStartupTests.setUpClass.__func__(WriteFreePublicRegressionTests)
        self.main = WriteFreePublicRegressionTests.main
        self.stack.enter_context(patch.object(self.main, 'model', self.model))
        self.stack.enter_context(patch.object(self.main, 'write_free_verifier', self.verifier))
        self.stack.enter_context(patch.object(self.main.app.state, 'startup_complete', False))
        clock = Clock()
        self.stack.enter_context(patch.object(self.main, 'startup', Startup(clock=clock, sleep=clock.sleep)))
        self.stack.enter_context(patch.object(self.main, 'configure_database_timeouts', side_effect=lambda engine, startup: engine))
        self.create = self.stack.enter_context(patch.object(self.main, 'create_engine'))
        self.runtime_engine = self.stack.enter_context(patch.object(self.main, 'engine'))
        self.metrics = {name: self.stack.enter_context(patch.object(self.main, name)) for name in WriteFreePublicRegressionTests.METRICS}

    async def asyncTearDown(self):
        await self.verifier.stop()

    async def private_request(self):
        import httpx
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.verifier.app), base_url='http://uds') as client:
            return await client.post('/verify/predict', json=WriteFreeUDSTests.PAYLOAD)

    async def test_wf13_existing_schema_read_precedes_bind_and_requests_are_db_free(self):
        trace = []
        connection = self.create.return_value.connect.return_value.__enter__.return_value
        connection.execute.side_effect = lambda *args: trace.append('schema-select')
        self.create.return_value.dispose.side_effect = lambda: trace.append('preflight-dispose')
        bind = self.verifier._bind
        def record_bind():
            self.assertTrue(self.main.app.state.startup_complete)
            trace.append('uds-bind')
            return bind()
        with patch.object(self.verifier, '_bind', side_effect=record_bind), patch('backend.model_loader.load_model', side_effect=AssertionError('duplicate model load')):
            self.assertFalse(self.socket_path.exists())
            async with self.main.app.router.lifespan_context(self.main.app):
                self.assertEqual(trace, ['schema-select', 'preflight-dispose', 'uds-bind'])
                connection.execute.assert_called_once()
                sql = str(connection.execute.call_args.args[0])
                self.assertTrue(sql.lstrip().startswith('SELECT '))
                self.assertIn('LIMIT 0', sql)
                self.create.return_value.begin.assert_not_called()
                self.create.return_value.reset_mock()
                self.assertEqual((await self.private_request()).status_code, 200)
                self.assertEqual(self.runtime_engine.mock_calls, [])
                self.assertEqual(self.create.return_value.mock_calls, [])
                for metric in self.metrics.values():
                    self.assertEqual(metric.mock_calls, [])
                self.assertEqual(self.main.readiness(), {'status': 'ready'})
                self.assertEqual(str(self.runtime_engine.connect.return_value.__enter__.return_value.execute.call_args.args[0]), 'SELECT 1')
            self.assertFalse(self.socket_path.exists())

    async def test_wf12_schema_startup_failure_prevents_verifier_lifecycle(self):
        connection = self.create.return_value.connect.return_value.__enter__.return_value
        connection.execute.side_effect = ProgrammingError('', {}, mysql.ProgrammingError(1146, SECRET))
        with patch.object(self.verifier, 'start') as start:
            with self.assertRaises(StartupError):
                async with self.main.app.router.lifespan_context(self.main.app):
                    self.fail('startup failure cannot enter a serving lifespan')
            start.assert_not_called()
        self.assertFalse(self.main.app.state.startup_complete)
        self.assertFalse(self.socket_path.exists())
        self.assertEqual((await self.private_request()).status_code, 503)

    async def test_wf10_missing_primary_model_prevents_verifier_start(self):
        with patch.object(self.main, 'model', None), patch.object(self.verifier, 'start') as start:
            with self.assertRaises(RuntimeError):
                async with self.main.app.router.lifespan_context(self.main.app):
                    self.fail('missing model cannot serve')
            start.assert_not_called()
        self.assertFalse(self.main.app.state.startup_complete)
        self.assertFalse(self.socket_path.exists())

    async def test_wf13_verifier_start_failure_clears_primary_ready_latch(self):
        with patch.object(self.vmod, 'EXPECTED_UID', os.geteuid() + 1):
            with self.assertRaises(self.vmod.VerifierError):
                async with self.main.app.router.lifespan_context(self.main.app):
                    self.fail('invalid verifier runtime cannot become ready')
        self.assertFalse(self.main.app.state.startup_complete)
        self.assertFalse(self.socket_path.exists())

    async def test_uds12_simultaneous_public_and_private_use_same_model_only_public_writes(self):
        import asyncio
        import httpx
        import threading
        barrier = threading.Barrier(2)
        original_predict = self.model.predict
        def simultaneous(features):
            barrier.wait(timeout=3)
            return original_predict(features)
        conn = self.runtime_engine.begin.return_value.__enter__.return_value
        conn.execute.return_value.lastrowid = 777
        async with self.main.app.router.lifespan_context(self.main.app):
            with patch.object(self.model, 'predict', side_effect=simultaneous) as calls, patch.object(type(self.model), 'fit', side_effect=AssertionError('fit')):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.main.app), base_url='http://public') as client:
                    private, public = await asyncio.gather(self.private_request(), client.post('/predict', json={**WriteFreeUDSTests.PAYLOAD, 'server_id': 'fixture'}))
            self.assertEqual(private.status_code, 200)
            self.assertEqual(public.status_code, 200)
            self.assertEqual(private.json()['prediction'], public.json()['prediction'])
            self.assertEqual(calls.call_count, 2)
            self.runtime_engine.begin.assert_called_once_with()
            conn.execute.assert_called_once()
            self.metrics['prediction_counter'].labels.return_value.inc.assert_called_once_with()
            self.metrics['anomaly_counter'].inc.assert_called_once_with()
            for name in ['cpu_gauge', 'memory_gauge', 'temperature_gauge', 'power_gauge']:
                self.metrics[name].labels.return_value.set.assert_called_once()


class WriteFreeExistingTargetBoundaryTests(unittest.TestCase):
    def test_uds08_existing_loader_rejects_wrong_dev_registry_target_before_network(self):
        env = dict(WriteFreeActivationTests.ENV)
        for key, value in [('MLFLOW_TRACKING_URI', 'http://mlflow.mlflow.svc.cluster.local:5000'), ('MLFLOW_TRACKING_URI', ''), ('MLFLOW_MODEL_VERSION', '2'), ('MLFLOW_MODEL_NAME', 'other-model')]:
            with self.subTest(key=key, value=value), patch.dict(os.environ, {**env, key: value}, clear=True), patch('mlflow.MlflowClient') as client, patch('mlflow.artifacts.download_artifacts') as download:
                with self.assertRaises(loader.ModelLoadError):
                    loader.load_model('unused', startup=Startup(clock=Clock(), sleep=lambda _: None))
                client.assert_not_called(); download.assert_not_called()


if __name__=='__main__':unittest.main()
