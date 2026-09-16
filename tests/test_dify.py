import json
import os
import unittest
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from backend.dify import run_analysis


class DifyTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'DIFY_BASE_URL': 'http://dify.test/v1',
                                          'DIFY_API_KEY': 'test-only-key',
                                          'DIFY_TIMEOUT_SECONDS': '120'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.context = {'summary': {'sample_count': 3}, 'scope': {'server_id': '서버01'}}
        self.calls = []

    def run_mock(self, status=200, payload=None, error=None, raw=None):
        def handler(request):
            self.calls.append(request)
            if error:
                raise error('private upstream detail', request=request)
            if raw is not None:
                return httpx.Response(status, content=raw)
            return httpx.Response(status, json=payload)
        real_client = httpx.Client
        def factory(**kwargs):
            self.assertFalse(kwargs['follow_redirects'])
            self.assertFalse(kwargs['trust_env'])
            self.assertEqual(kwargs['timeout'].connect, 5)
            return real_client(transport=httpx.MockTransport(handler), **kwargs)
        with patch('backend.dify.httpx.Client', side_effect=factory):
            return run_analysis(self.context)

    def test_success_payload_and_outputs(self):
        outputs = {'custom_result': {'text': '분석'}}
        result = self.run_mock(payload={'workflow_run_id': 'run1',
                              'data': {'status': 'succeeded', 'outputs': outputs}})
        self.assertEqual(result['outputs'], outputs)
        request = self.calls[0]
        self.assertEqual(str(request.url), 'http://dify.test/v1/workflows/run')
        body = json.loads(request.content)
        self.assertEqual(json.loads(body['inputs']['incident_context']), self.context)
        self.assertEqual(body['response_mode'], 'blocking')
        self.assertEqual(request.headers['Authorization'], 'Bearer test-only-key')
        self.assertEqual(len(self.calls), 1)

    def test_zero_samples_skips_without_config_or_client(self):
        with patch.dict(os.environ, {}, clear=True), patch('backend.dify.httpx.Client') as client:
            result = run_analysis({'summary': {'sample_count': 0, 'has_data': True}})
        self.assertEqual(result['reason'], 'no_data')
        client.assert_not_called()

    def test_missing_and_invalid_config(self):
        for values in [{'DIFY_API_KEY': ''}, {'DIFY_TIMEOUT_SECONDS': 'nan'},
                       {'DIFY_TIMEOUT_SECONDS': 'bad'}, {'DIFY_BASE_URL': ''},
                       {'DIFY_API_KEY': 'bad\nkey'}]:
            with self.subTest(values=list(values)), patch.dict(os.environ, values), patch('backend.dify.httpx.Client') as client:
                with self.assertRaises(HTTPException) as caught:
                    run_analysis(self.context)
                self.assertEqual(caught.exception.status_code, 503)
                client.assert_not_called()

    def test_http_errors_are_sanitized(self):
        for status in [401, 403, 429, 500, 302]:
            with self.subTest(status=status):
                with self.assertRaises(HTTPException) as caught:
                    self.run_mock(status=status, payload={'error': 'private upstream detail'})
                self.assertEqual(caught.exception.status_code, 502)
                self.assertNotIn('private', caught.exception.detail)
                if status in (401, 403):
                    self.assertEqual(caught.exception.detail, 'Dify authentication failed')

    def test_timeout_and_connection_failure_no_retry(self):
        for error, expected in [(httpx.ReadTimeout, 504), (httpx.ConnectError, 502)]:
            self.calls.clear()
            with self.assertRaises(HTTPException) as caught:
                self.run_mock(error=error)
            self.assertEqual(caught.exception.status_code, expected)
            self.assertEqual(len(self.calls), 1)
            self.assertNotIn('private', caught.exception.detail)

    def test_failed_and_malformed_workflows(self):
        for payload in [None, [], {}, {'data': {'status': 'failed', 'error': 'private'}},
                        {'data': {'status': 'running'}},
                        {'workflow_run_id': 'r', 'data': {'status': 'succeeded', 'outputs': None}}]:
            with self.subTest(payload=payload), self.assertRaises(HTTPException) as caught:
                self.run_mock(payload=payload)
            self.assertEqual(caught.exception.status_code, 502)
            self.assertNotIn('private', caught.exception.detail)
        with self.assertRaises(HTTPException) as caught:
            self.run_mock(raw=b'not json')
        self.assertEqual(caught.exception.status_code, 502)
