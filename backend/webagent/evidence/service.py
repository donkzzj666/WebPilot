"""Trusted capture publication and evidence-qualified model inputs.

There is deliberately no HTTP publication or raw-file read capability. A
FILTERED label is meaningful only with this domain's immutable artifact index.
"""
from copy import deepcopy
from pathlib import Path
import struct

from ..db import connect, transaction
from ..db.repository import canonical_json, utc_text
from ..errors import BusinessError
from ..models.schema import Observation
from ..models.transport import ModelImage
from .redaction import (TextRedactor, ScreenshotRedactor, filter_model_text,
                        is_neutral_png, safe_metadata, safe_url)
from .store import EvidenceStore
from .models import unavailable

POLICY_VERSION = 'm1-14-text-v1-raster-blocked-v1'
_BINDING = ('snapshot_id', 'run_id', 'source_url', 'tab_id', 'frame_id',
            'page_version', 'width', 'height')


def blocked():
    return BusinessError('INPUT_BLOCKED', 'Evidence cannot cross the model boundary', status=409)


class EvidenceService:
    def __init__(self, data_dir: Path, *, store=None, known_secrets=()):
        self.store = store or EvidenceStore(Path(data_dir))
        self.text = TextRedactor(tuple(known_secrets))
        self.known_secrets = tuple(known_secrets)

    def check_storage_error(self, error):
        """Shared media failure also fences journals outside artifact writes."""
        if self.store._full(error):
            self.store._storage_fault()
            raise unavailable() from None

    def publish_observation(self, observation: dict, raw_capture: dict, *,
                            execution_token=None, expected_state_version=None,
                            mask_screenshot=False) -> dict:
        """Internal capture adapter; caller labels and artifact IDs are ignored.

        Non-gateway adapters may register a trusted snapshot here. Actual model
        calls still require its persisted binding and reverified artifact bytes.
        """
        if type(observation) is not dict or type(raw_capture) is not dict or type(mask_screenshot) is not bool:
            raise blocked()
        self.store.assert_dispatch_allowed()
        content = {key: observation[key] for key in _BINDING}
        image = raw_capture.get('screenshot')
        if image is not None:
            ScreenshotRedactor().filter(image)
            if struct.unpack('>II', image[16:24]) != (content['width'], content['height']):
                raise blocked()
        for key in ('source_url', 'snapshot_id', 'run_id', 'tab_id', 'frame_id', 'page_version'):
            value = content[key]
            if type(value) is not str or self.text.contains_sensitive(value):
                raise blocked()
        content.update(captured_at=observation.get('captured_at', utc_text()),
            title=self.text.filter(raw_capture.get('title', observation.get('title', ''))),
            visible_excerpt=self.text.filter(raw_capture.get('text', raw_capture.get('visible_text',
                raw_capture.get('visible_excerpt', observation.get('visible_excerpt', '')))))[:16384],
            redaction_status='FILTERED', evidence_ids=[])
        # Validate bounded shape before any publication. This also fixes UTC
        # precision without ever persisting the raw title/body in a DB row.
        dto = Observation.model_validate_json(canonical_json(content))
        content = dto.model_dump(mode='json')
        with connect(self.store.database) as db, transaction(db):
            self.store._qualify(db, content['run_id'], execution_token, expected_state_version)
            if not db.execute('SELECT 1 FROM evidence_run_guards WHERE run_id=?', (content['run_id'],)).fetchone():
                db.execute('INSERT INTO evidence_run_guards VALUES(?,?)', (content['run_id'], utc_text()))
            current = db.execute('SELECT * FROM observations WHERE snapshot_id=?',
                                 (content['snapshot_id'],)).fetchone()
            if current is None:
                db.execute('''INSERT INTO observations(snapshot_id,run_id,captured_at,source_url,title,
                    tab_id,frame_id,page_version,width,height,visible_excerpt,redaction_status)
                    VALUES(?,?,?,?,?,?,?,?,?,?,'','BLOCKED')''',
                    (content['snapshot_id'], content['run_id'], utc_text(dto.captured_at), content['source_url'],
                     '[private observation]', content['tab_id'], content['frame_id'], content['page_version'],
                     content['width'], content['height']))
            elif (any(current[key] != content[key] for key in _BINDING)
                  or current['captured_at'] != utc_text(dto.captured_at)):
                raise blocked()
        common = dict(source_url=content['source_url'], captured_at=dto.captured_at,
            object_id=content['snapshot_id'], query_scope='current viewport',
            locator_or_page=canonical_json({key: content[key] for key in ('tab_id', 'frame_id', 'page_version')}),
            snapshot_id=content['snapshot_id'], execution_token=execution_token,
            expected_state_version=expected_state_version)
        original_envelope = {'title': raw_capture.get('title', observation.get('title', '')),
            'text': raw_capture.get('text', raw_capture.get('visible_text',
                raw_capture.get('visible_excerpt', observation.get('visible_excerpt', ''))))}
        if 'text_truncated' in raw_capture:
            if type(raw_capture['text_truncated']) is not bool:
                raise blocked()
            original_envelope['text_truncated'] = raw_capture['text_truncated']
        raw_text = canonical_json(original_envelope).encode('utf-8')
        original = self.store.publish(content['run_id'], raw_text, **common)
        display_text = canonical_json({'title': content['title'], 'text': content['visible_excerpt']})
        filtered = self.store.publish(content['run_id'], display_text.encode('utf-8'), **common,
            sensitivity='redacted', original_evidence_id=original['evidence_id'],
            excerpt=content['visible_excerpt'], redaction_status='FILTERED', policy_version=POLICY_VERSION)
        content['evidence_ids'].append(filtered['evidence_id'])
        if image is not None:
            original_image = self.store.publish(content['run_id'], image, **common,
                artifact_kind='screenshot', evidence_id=observation.get('screenshot_evidence_id'))
            # Opaque images, canvas text, CSS, and scanned documents are not
            # classified by DOM inspection. Default policy preserves locally
            # and blocks transfer. An explicit full mask is an inert derivative.
            if mask_screenshot:
                masked = ScreenshotRedactor().mask_all_png(image)
                derivative = self.store.publish(content['run_id'], masked.data, **common,
                    artifact_kind='screenshot', sensitivity='redacted',
                    original_evidence_id=original_image['evidence_id'], redaction_status='FILTERED',
                    policy_version=POLICY_VERSION)
                content['evidence_ids'].append(derivative['evidence_id'])
        return self.store.record_filtered_observation(content['snapshot_id'], content,
            policy_version=POLICY_VERSION, evidence_ids=content['evidence_ids'],
            execution_token=execution_token, expected_state_version=expected_state_version)['content']

    def publish_result(self, run_id, snapshot_id, step_id, result, *, execution_token=None, source_url=None):
        """Preserve action artifacts before a completed journal can be exposed."""
        if type(result) is not dict:
            raise blocked()
        with connect(self.store.database) as db:
            snapshot = db.execute('SELECT * FROM observations WHERE run_id=? AND snapshot_id=?',
                                 (run_id, snapshot_id)).fetchone()
            if snapshot is None:
                raise blocked()
        source = source_url or snapshot['source_url']
        if self.text.contains_sensitive(source):
            raise blocked()
        common = dict(source_url=source, captured_at=utc_text(),
            object_id=step_id, query_scope='structured browser action', locator_or_page=step_id,
            step_id=step_id, snapshot_id=snapshot_id, execution_token=execution_token)
        data = result.get('artifact_bytes')
        if data is not None:
            kind = 'screenshot' if result.get('artifact_kind') == 'screenshot' else (
                'pdf' if data.startswith(b'%PDF-') else 'text')
            if kind == 'screenshot':
                ScreenshotRedactor().filter(data)
                if struct.unpack('>II', data[16:24]) != (snapshot['width'], snapshot['height']):
                    raise blocked()
            self.store.publish(run_id, data, **common, artifact_kind=kind)
        # Even non-artifact actions have an immutable restricted receipt. The
        # redacted derivative retains bounded journal data, never raw pixels.
        def receipt(value):
            if type(value) is bytes:
                import hashlib
                return {'sha256': hashlib.sha256(value).hexdigest(), 'size_bytes': len(value)}
            if type(value) is dict:
                return {key: receipt(item) for key, item in value.items()}
            if type(value) in (list, tuple):
                return [receipt(item) for item in value]
            return value
        raw = canonical_json(receipt(result)).encode('utf-8')
        original = self.store.publish(run_id, raw, **common)
        filtered = self.text.filter(raw.decode('utf-8'))
        return self.store.publish(run_id, filtered.encode('utf-8'), **common,
            sensitivity='redacted', original_evidence_id=original['evidence_id'],
            redaction_status='FILTERED', policy_version=POLICY_VERSION)

    def model_images(self, snapshot_id, evidence_ids, *, run_id):
        view = self.store.filtered_observation(snapshot_id, run_id)['content']
        if len(evidence_ids) != len(set(evidence_ids)) or not set(evidence_ids) <= set(view['evidence_ids']):
            raise blocked()
        result = []
        for identifier in evidence_ids:
            metadata, data = self.store.read(identifier, run_id=run_id)
            if (metadata['snapshot_id'] != snapshot_id or metadata['artifact_kind'] != 'screenshot'
                    or metadata['policy_version'] != POLICY_VERSION or not is_neutral_png(data)):
                raise blocked()
            if struct.unpack('>II', data[16:24]) != (view['width'], view['height']):
                raise blocked()
            result.append(ModelImage(identifier, data, 'image/png'))
        return tuple(result)

    def guard_model_input(self, payload: dict, images=(), *, known_secrets=()) -> dict:
        self.store.assert_dispatch_allowed()
        try:
            observation, run_id = payload['observation'], payload['run_id']
            view = self.store.filtered_observation(observation['snapshot_id'], run_id)
            if view['policy_version'] != POLICY_VERSION or observation != view['content']:
                raise blocked()
            with connect(self.store.database) as db:
                row = db.execute('SELECT * FROM observations WHERE snapshot_id=?',
                                 (observation['snapshot_id'],)).fetchone()
                if row is None or any(row[key] != observation[key] for key in _BINDING):
                    raise blocked()
            for identifier in observation['evidence_ids']:
                metadata, _ = self.store.read(identifier, run_id=run_id)
                if metadata['snapshot_id'] != observation['snapshot_id'] or metadata['policy_version'] != POLICY_VERSION:
                    raise blocked()
            checkpoint = payload.get('verified_checkpoint')
            if checkpoint:
                for identifier in checkpoint.get('evidence_ids', ()):
                    self.store.read(identifier, run_id=run_id)
            verified = self.model_images(observation['snapshot_id'], payload['image_evidence_ids'], run_id=run_id)
            if tuple(images) != verified:
                raise blocked()
            return filter_model_text(deepcopy(payload), known_secrets=tuple(known_secrets) + self.known_secrets)
        except BusinessError as error:
            if error.code == 'EVIDENCE_STORAGE_UNAVAILABLE':
                raise
            raise blocked() from None

    def display(self, evidence_id):
        metadata, data = self.store.read(evidence_id)
        if metadata['artifact_kind'] not in ('text', 'diff', 'screenshot'):
            raise BusinessError('FORBIDDEN', 'Artifact has no approved display format', status=403)
        if metadata['artifact_kind'] == 'screenshot' and not is_neutral_png(data):
            raise BusinessError('FORBIDDEN', 'Image has no verified display copy', status=403)
        if metadata['artifact_kind'] in ('text', 'diff'):
            try:
                value = data.decode('utf-8')
            except UnicodeError:
                raise blocked() from None
            if self.text.contains_sensitive(value):
                raise BusinessError('FORBIDDEN', 'Text has no verified display copy', status=403)
        # Re-filter metadata even for an incorrectly used internal publisher.
        public = {key: value for key, value in metadata.items() if key != 'artifact_path'}
        for key in ('object_id', 'query_scope', 'locator_or_page', 'excerpt'):
            public[key] = safe_metadata(public[key], known_secrets=self.known_secrets)
        public['source_url'] = safe_url(public['source_url'], known_secrets=self.known_secrets)
        return public, data
