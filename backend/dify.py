"""Dify blocking Workflow API adapter. Never log credentials or upstream errors."""
import json
import math
import os
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException


def run_analysis(context: dict) -> dict:
    if context['summary']['sample_count'] == 0:
        return {'status': 'skipped', 'reason': 'no_data', 'context': context,
                'workflow_run_id': None, 'outputs': None}

    base_url = os.getenv('DIFY_BASE_URL', '').strip().rstrip('/')
    key = os.getenv('DIFY_API_KEY', '')
    try:
        url = urlsplit(base_url)
        port = url.port  # Access validates nonnumeric and out-of-range ports.
        timeout = float(os.getenv('DIFY_TIMEOUT_SECONDS', '120'))
        valid = (url.scheme in ('http', 'https') and url.hostname
                 and port != 0 and not url.netloc.endswith(':')
                 and not url.username and not url.password
                 and not url.query and not url.fragment
                 and math.isfinite(timeout) and 0 < timeout <= 300
                 and key and key == key.strip() and key.isascii()
                 and all(32 < ord(c) < 127 for c in key))
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(503, 'Dify configuration unavailable')

    try:
        # No retry: a timeout can occur after the Workflow has already started.
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=5.0),
                          follow_redirects=False, trust_env=False) as client:
            response = client.post(
                f'{base_url}/workflows/run',
                headers={'Authorization': f'Bearer {key}'},
                json={'inputs': {'incident_context': json.dumps(context, ensure_ascii=False)},
                      'response_mode': 'blocking', 'user': 'datacenter-api'},
            )
            if response.status_code in (401, 403):
                raise HTTPException(502, 'Dify authentication failed')
            response.raise_for_status()
    except httpx.InvalidURL:
        raise HTTPException(503, 'Dify configuration unavailable') from None
    except httpx.TimeoutException:
        raise HTTPException(504, 'Dify request timed out; execution may still be running') from None
    except httpx.HTTPError:
        raise HTTPException(502, 'Dify request failed') from None

    try:
        payload = response.json()
        data = payload['data']
        if data['status'] != 'succeeded':
            raise HTTPException(502, 'Dify workflow failed')
        outputs = data['outputs']
        run_id = payload['workflow_run_id']
        if not isinstance(outputs, dict) or not isinstance(run_id, str) or not run_id:
            raise ValueError('invalid workflow response')
        analysis_result = outputs.get('analysis_result')
        if not isinstance(analysis_result, str) or not analysis_result.strip():
            raise ValueError('invalid analysis result')
    except (ValueError, KeyError, TypeError):
        raise HTTPException(502, 'Dify returned an invalid response') from None
    return {'status': 'succeeded', 'context': context,
            'workflow_run_id': run_id, 'outputs': outputs}
