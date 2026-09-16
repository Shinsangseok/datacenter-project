"""Offline regression tests. No RDS connection or model file loading."""
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

os.environ.setdefault('DB_HOST', 'localhost')
os.environ.setdefault('DB_NAME', 'test')
os.environ.setdefault('DB_USER', 'test')
os.environ.setdefault('DB_PASSWORD', 'test')
with patch('joblib.load', return_value=MagicMock()):
    from backend import main
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError


class ContextTests(unittest.TestCase):
    def context(self, count=3, anomalies=1, rows=None):
        summary = dict(sample_count=count, anomaly_count=anomalies,
                       latest_measured_at=datetime(2026, 9, 16) if count else None)
        for metric in ['cpu', 'memory', 'temperature', 'power']:
            for aggregate in ['avg', 'max']:
                summary[f'{aggregate}_{metric}'] = 12.345 if count else None
        connection = MagicMock()
        first, second = MagicMock(), MagicMock()
        first.mappings.return_value.one.return_value = summary
        second.mappings.return_value.all.return_value = rows or []
        connection.execute.side_effect = [first, second]
        with patch.object(main, 'engine') as engine:
            engine.connect.return_value.__enter__.return_value = connection
            result = main.build_analysis_context('server01', 30, 5)
        return result, connection

    def test_window_and_precision(self):
        result, connection = self.context()
        self.assertTrue(result['summary']['has_data'])
        self.assertEqual(result['summary']['anomaly_rate'], 1 / 3)
        for call in connection.execute.call_args_list:
            sql, params = call.args
            self.assertIn('measured_at >= :since', str(sql))
            self.assertIn('measured_at <= :until', str(sql))
            self.assertEqual(params['server_id'], 'server01')
            self.assertIsNone(params['until'].tzinfo)
            self.assertEqual((params['until'] - params['since']).total_seconds(), 1800)
        self.assertEqual(connection.execute.call_args_list[1].args[1]['anomaly_limit'], 5)
        self.assertTrue(result['summary']['latest_measured_at'].endswith('+00:00'))

    def test_empty(self):
        result, _ = self.context(0, 0)
        self.assertFalse(result['summary']['has_data'])
        self.assertEqual(result['summary']['anomaly_rate'], 0)
        self.assertIsNone(result['summary']['max_cpu'])
        self.assertIsNone(result['summary']['latest_measured_at'])
        self.assertEqual(result['recent_anomalies'], [])

    def test_anomaly_timestamp(self):
        row = dict(id=1, measured_at=datetime(2026, 9, 16), server_id='server01',
                   cpu=99, memory=90, temperature=80, power=300,
                   prediction='ANOMALY', probability=0.9)
        result, _ = self.context(rows=[row])
        stamp = result['recent_anomalies'][0]['measured_at']
        self.assertEqual(datetime.fromisoformat(stamp).tzinfo, timezone.utc)

    def test_database_failure_is_sanitized(self):
        with patch.object(main, 'engine') as engine:
            engine.connect.side_effect = SQLAlchemyError('sensitive database detail')
            with self.assertRaises(main.HTTPException) as error:
                main.build_analysis_context(None, 30, 5)
        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(error.exception.detail, 'could not build analysis context')

    def test_http_validation(self):
        client = TestClient(main.app)  # startup DB creation is deliberately not run
        with patch.object(main, 'build_analysis_context') as build:
            for query in ['minutes=0', 'minutes=1441', 'anomaly_limit=0', 'anomaly_limit=21']:
                self.assertEqual(client.get('/analysis/context?' + query).status_code, 422)
            build.assert_not_called()
            build.return_value = {'summary': {'has_data': False}}
            self.assertEqual(client.get('/analysis/context').status_code, 200)
            build.assert_called_once_with(server_id=None, minutes=30, anomaly_limit=5)

    def test_analysis_endpoint_skip_and_validation(self):
        client = TestClient(main.app)
        with patch.object(main, 'build_analysis_context') as build, patch('backend.dify.httpx.Client') as outbound:
            build.return_value = {'summary': {'sample_count': 0}}
            result = client.post('/analysis/run', json={})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()['status'], 'skipped')
            outbound.assert_not_called()
            build.reset_mock()
            for body in [{'minutes': 0}, {'anomaly_limit': 21}, {'server_id': ''}, {'extra': True}]:
                self.assertEqual(client.post('/analysis/run', json=body).status_code, 422)
            build.assert_not_called()

    def test_analysis_endpoint_uses_context_and_propagates_errors(self):
        client = TestClient(main.app)
        context = {'summary': {'sample_count': 2}}
        with patch.object(main, 'build_analysis_context', return_value=context) as build, patch.object(main, 'run_analysis') as run:
            run.return_value = {'status': 'succeeded', 'outputs': {'custom': 'result'}}
            result = client.post('/analysis/run', json={'server_id': 'server01', 'minutes': 10, 'anomaly_limit': 2})
            self.assertEqual(result.status_code, 200)
            build.assert_called_once_with(server_id='server01', minutes=10, anomaly_limit=2)
            run.assert_called_once_with(context)
            for code in [502, 503, 504]:
                run.side_effect = main.HTTPException(code, 'safe error')
                self.assertEqual(client.post('/analysis/run', json={}).status_code, code)
            run.reset_mock()
            build.side_effect = main.HTTPException(503, 'could not build analysis context')
            self.assertEqual(client.post('/analysis/run', json={}).status_code, 503)
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
