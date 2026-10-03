"""Authenticated ID-only evidence reads never expose private originals or paths."""
from datetime import datetime, timezone
import os

import pytest

from api_support import AuthenticatedTestClient, create_test_app
from webagent.config import Settings
from webagent.db import connect, transaction
from webagent.evidence.store import EvidenceStore
from conftest import seed


SECRET = 'SYNTHETIC_API_KEY_NEVER_EXPORT_731'


@pytest.fixture
def rig(database):
    with connect(database) as db, transaction(db):
        seed(db)
    store = EvidenceStore(database.parent)
    options = dict(source_url='https://fixture.invalid/report',
                   captured_at=datetime.now(timezone.utc), object_id='fixture-report',
                   query_scope='fixture', locator_or_page='page 1')
    original = store.publish('run-1', ('api_key=' + SECRET).encode(), **options)
    display = store.publish('run-1', b'Revenue: 42 USD\napi_key=[REDACTED]', **options,
                            original_evidence_id=original['evidence_id'],
                            sensitivity='redacted', redaction_status='FILTERED',
                            policy_version='m1-14-text-v1')
    with AuthenticatedTestClient(create_test_app(Settings(database.parent))) as client:
        yield client, store, original, display


def test_metadata_and_content_read_verified_derivative_with_safe_headers(rig):
    client, store, original, display = rig
    response = client.get('/v1/evidence/' + display['evidence_id'])
    assert response.status_code == 200
    payload = response.json()
    assert payload['evidence_id'] == display['evidence_id']
    assert payload['sha256'] == display['sha256']
    assert payload['original_evidence_id'] == original['evidence_id']
    assert 'artifact_path' not in payload and str(store.data_dir) not in response.text
    assert SECRET not in response.text
    body = client.get('/v1/evidence/' + display['evidence_id'] + '/content')
    assert body.status_code == 200
    assert body.content == b'Revenue: 42 USD\napi_key=[REDACTED]'
    assert body.headers['content-type'].startswith('text/plain')
    for result in (response, body):
        assert result.headers['cache-control'] == 'no-store'
        assert result.headers['x-content-type-options'] == 'nosniff'
        assert result.headers['x-frame-options'] == 'DENY'
        assert 'access-control-allow-origin' not in result.headers


@pytest.mark.parametrize('suffix', ['', '/content'])
def test_private_original_has_no_http_read_mode(rig, suffix):
    client, _, original, _ = rig
    response = client.get('/v1/evidence/' + original['evidence_id'] + suffix)
    assert response.status_code == 403
    assert SECRET not in response.text and 'artifact_path' not in response.text


@pytest.mark.parametrize('query', ['path=/etc/passwd', 'raw=true', 'run_id=run-1',
    'download=true', 'original=true', 'path=../business.sqlite3', 'raw=1&raw=0'])
@pytest.mark.parametrize('suffix', ['', '/content'])
def test_unknown_query_modes_cannot_bypass_id_only_read(rig, query, suffix):
    client, _, _, display = rig
    response = client.get('/v1/evidence/' + display['evidence_id'] + suffix + '?' + query)
    assert response.status_code == 422
    assert SECRET not in response.text and '/etc/passwd' not in response.text


@pytest.mark.parametrize('identifier', ['..', '%2e%2e', '%2e%2e%2fbusiness.sqlite3',
    '%252e%252e%252fetc%252fpasswd', '%00', 'evidence%5c..%5cbusiness.sqlite3',
    'a' * 201, 'not-published'])
def test_invalid_or_unknown_identifier_never_selects_a_local_path(rig, identifier):
    client, _, _, _ = rig
    response = client.get('/v1/evidence/' + identifier + '/content')
    assert response.status_code in (404, 422)
    assert SECRET not in response.text and 'root:' not in response.text


@pytest.mark.parametrize('headers,status', [({'Authorization': ''}, 401),
    ({'Origin': 'https://attacker.invalid'}, 403), ({'Host': 'attacker.invalid'}, 403),
    ({'Sec-Fetch-Site': 'cross-site'}, 403)])
@pytest.mark.parametrize('suffix', ['', '/content'])
def test_local_security_boundary_applies_to_all_evidence_routes(rig, headers, status, suffix):
    client, _, _, display = rig
    response = client.get('/v1/evidence/' + display['evidence_id'] + suffix, headers=headers)
    assert response.status_code == status and SECRET not in response.text


@pytest.mark.parametrize('mutation', ['missing', 'corrupt', 'symlink'])
def test_unreadable_artifact_cannot_be_exported_and_failure_remains_indexed(rig, mutation):
    client, store, _, display = rig
    artifact = store.data_dir / display['artifact_path']
    if mutation == 'missing':
        artifact.unlink()
    elif mutation == 'corrupt':
        artifact.write_bytes(b'corrupt synthetic bytes')
    else:
        artifact.unlink()
        os.symlink(store.database, artifact)
    response = client.get('/v1/evidence/' + display['evidence_id'] + '/content')
    assert response.status_code == 409
    assert response.json()['code'] in ('EVIDENCE_MISSING', 'EVIDENCE_CORRUPT')
    assert 'SQLite format' not in response.text and SECRET not in response.text
    assert store.metadata(display['evidence_id'])['capture_status'] == 'COMPLETE'
    assert store.metadata(display['evidence_id'])['availability'] == (
        'MISSING' if mutation == 'missing' else 'CORRUPT')


def test_expired_copy_is_never_exported(rig):
    client, store, _, display = rig
    store.expire(display['evidence_id'])
    response = client.get('/v1/evidence/' + display['evidence_id'] + '/content')
    assert response.status_code == 410 and response.json()['code'] == 'EVIDENCE_EXPIRED'


def test_active_binary_formats_have_no_generic_content_export(rig):
    client, store, _, _ = rig
    item = store.publish('run-1', b'{"synthetic":"trace"}',
        source_url='https://fixture.invalid/object', captured_at=datetime.now(timezone.utc),
        object_id='trace', query_scope='fixture', locator_or_page='trace', artifact_kind='har',
        sensitivity='public', redaction_status='FILTERED', policy_version='fixture')
    result = client.get('/v1/evidence/' + item['evidence_id'] + '/content')
    assert result.status_code == 403 and b'synthetic' not in result.content


def test_incorrect_internal_metadata_is_refiltered_before_display(rig):
    client, store, _, _ = rig
    item = store.publish('run-1', b'Public revenue: 42 USD',
        source_url='https://fixture.invalid/report?api_key=' + SECRET + '&year=2025',
        captured_at=datetime.now(timezone.utc), object_id='password=' + SECRET,
        query_scope='Cookie: session=' + SECRET, locator_or_page='Authorization: Bearer ' + SECRET,
        excerpt='api_key=' + SECRET, sensitivity='public', redaction_status='FILTERED',
        policy_version='fixture')
    response = client.get('/v1/evidence/' + item['evidence_id'])
    assert response.status_code == 200
    assert SECRET not in response.text and '[REDACTED]' in response.text
    assert response.json()['source_url'] == 'https://fixture.invalid/report?year=2025'
    assert response.json()['sha256'] == item['sha256']


def test_incorrect_internal_filtered_label_does_not_export_sensitive_text(rig):
    client, store, _, _ = rig
    item = store.publish('run-1', ('api_key=' + SECRET).encode(),
        source_url='https://fixture.invalid/object', captured_at=datetime.now(timezone.utc),
        object_id='object', query_scope='fixture', locator_or_page='page 1',
        sensitivity='public', redaction_status='FILTERED', policy_version='fixture')
    response = client.get('/v1/evidence/' + item['evidence_id'] + '/content')
    assert response.status_code == 403 and SECRET not in response.text
