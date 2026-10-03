"""M1-20 probe helpers preserve safe evidence and actual HTTP compiler scope."""
from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
import sys

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts' / 'verification'))
probe = importlib.import_module('verify_task_entry')


@pytest.mark.parametrize('method,path,status,expected', [
    ('POST', '/v1/tasks?api_key=PRIVATE', 422, {'method': 'POST', 'path': '/v1/tasks', 'status': 422}),
    ('GET', '/v1/settings?token=PRIVATE', 200, {'method': 'GET', 'path': '/v1/settings', 'status': 200}),
    ('GET', '/health', 200, {'method': 'GET', 'path': '/health', 'status': 200}),
    ('DELETE', '/v1/tasks/PRIVATE', 403, {'kind': 'other_request', 'status': 403}),
    ('GET', '/unknown/PRIVATE', 404, {'kind': 'other_request', 'method': 'GET', 'status': 404}),
    ('GET', '/v1/settings', 'PRIVATE', {'method': 'GET', 'path': '/v1/settings', 'status': 0}),
])
def test_request_summary_excludes_query_body_and_unknown_routes(method, path, status, expected):
    assert probe.request_summary(method, path, status) == expected
    assert 'PRIVATE' not in json.dumps(expected)


def test_secret_scan_returns_names_and_paths_only(tmp_path):
    (tmp_path / 'app.log').write_text('credential CANARY_SECRET\n')
    (tmp_path / 'safe.json').write_text('{}')
    private = tmp_path / '.security'
    private.mkdir()
    (private / 'local-api.token').write_text('LOCAL_TOKEN')
    (tmp_path / 'report.json').write_text('LOCAL_TOKEN')
    result = probe.scan_secrets(tmp_path, {'key': 'CANARY_SECRET', 'token': 'LOCAL_TOKEN'})
    assert result == {'passed': False, 'scanned_files': 2, 'canary_count': 2,
                      'matches': [{'artifact': 'app.log', 'canary_names': ['key']}]}
    assert 'CANARY_SECRET' not in json.dumps(result) and 'LOCAL_TOKEN' not in json.dumps(result)


def test_public_artifacts_exclude_security_private_and_report(tmp_path):
    for name in ('logs/a.jsonl', '.security/token', '.private/raw', 'report.json'):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('data')
    assert [str(p.relative_to(tmp_path)) for p in probe.public_artifacts(tmp_path)] == ['logs/a.jsonl']


def test_safe_error_does_not_export_message_or_locals():
    try:
        raise ValueError('SECRET reasoning and credentials')
    except ValueError as error:
        result = probe.safe_error(error)
    assert result['type'] == 'ValueError'
    assert result['frames'][-1]['function'] == 'test_safe_error_does_not_export_message_or_locals'
    assert 'SECRET' not in json.dumps(result)


def test_wait_until_is_bounded_and_propagates_owned_failure():
    async def exercise():
        async def never():
            return False
        with pytest.raises(TimeoutError):
            await probe.wait_until(never, 'owned fixture', timeout=.01)
        async def failed():
            raise RuntimeError('owned failure')
        with pytest.raises(RuntimeError):
            await probe.wait_until(failed, 'owned fixture', timeout=.01)
    asyncio.run(exercise())


def test_model_fixture_runs_real_http_without_exporting_prompt_or_credentials():
    async def exercise():
        model = await probe.SyntheticModel('SYNTHETIC_KEY_PRIVATE', 'REASONING_PRIVATE').start()
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.post(f'http://127.0.0.1:{model.port}/chat/completions',
                    headers={'Authorization': 'Bearer SYNTHETIC_KEY_PRIVATE'},
                    json={'model': 'deepseek-flash', 'response_format': {'type': 'json_object'}, 'stream': False,
                          'messages': [{'role': 'system', 'content': 'm1-06-compiler-v1'},
                                       {'role': 'user', 'content': json.dumps({'instruction': 'TASK_PRIVATE',
                                        'explicit_parameters': {}, 'explicit_scenario': None, 'web_context': []})}]})
                assert response.status_code == 200
                proposed = json.loads(response.json()['choices'][0]['message']['content'])
                assert proposed == {'scenario': 'finance', 'parameters': probe.PARAMETERS, 'ambiguous_fields': []}
            assert len(model.requests) == 1 and model.requests[0]['synthetic_authorization_matched']
            summary = json.dumps(model.requests)
            assert all(private not in summary for private in ('TASK_PRIVATE', 'SYNTHETIC_KEY_PRIVATE', 'REASONING_PRIVATE'))
            assert not model.errors
        finally:
            await model.close()
        assert not model.active
    asyncio.run(exercise())
