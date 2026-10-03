"""Trusted bounded result queries and evidence-backed write confirmations.

Only a locally installed adapter receives this surface. Public DTOs cannot
install adapters or pass code; the surface has observations and scoped GETs.
"""
from dataclasses import dataclass
import asyncio
import json
from uuid import uuid4

from ..db.repository import utc_text
from ..errors import BusinessError
from ..evidence.redaction import TextRedactor
from .models import WriteCheckFacts
from .store import WriteProtocolStore


@dataclass(frozen=True)
class WriteCheckCapture:
    data: bytes
    source_url: str
    snapshot_id: str


class WriteCheckSurface:
    """The adapter's fixed-token, fixed-operation read-only browser vocabulary."""
    def __init__(self, gateway, token, operation_id):
        self._gateway, self._token, self._operation_id = gateway, token, operation_id
        self._snapshots = set()

    async def observe(self):
        snapshot = await self._gateway._observe(self._token, write_operation_id=self._operation_id)
        self._snapshots.add(snapshot['snapshot_id'])
        return snapshot

    async def navigate(self, url):
        await self._gateway._write_check_navigate(self._token, url, self._operation_id)
        return await self.observe()

    def capture(self, snapshot_id):
        if snapshot_id not in self._snapshots:
            raise BusinessError('FORBIDDEN', 'Write query capture belongs to another check', status=403)
        # Raw, bounded Worker data never enters public diagnostics or the model.
        return self._gateway.local_result(snapshot_id)

    async def assert_current(self, snapshot_id):
        if snapshot_id not in self._snapshots:
            raise BusinessError('FORBIDDEN', 'Write proof requires an observation from this query', status=403)
        await self._gateway._assert_write_check_snapshot(self._token, self._operation_id, snapshot_id)


class WriteVerificationService:
    def __init__(self, path, evidence, *, store=None):
        self.path, self.evidence = path, evidence
        self.store = store or WriteProtocolStore(path)

    async def check(self, token, operation_id, adapter, surface, *, check_id=None):
        operation = await asyncio.to_thread(self.store.get, operation_id)
        captured = await adapter(operation, surface)
        if (type(captured) is not WriteCheckCapture or type(captured.data) is not bytes
                or not 1 <= len(captured.data) <= 32768
                or type(captured.source_url) is not str or type(captured.snapshot_id) is not str
                or TextRedactor().contains_sensitive(captured.source_url)):
            raise BusinessError('INVALID_PARAMETER', 'Trusted write query returned invalid bounded evidence')
        try:
            facts = json.loads(captured.data)
            WriteCheckFacts.model_validate(facts)
        except (ValueError, TypeError):
            raise BusinessError('INVALID_PARAMETER', 'Trusted write query returned invalid facts') from None
        if facts['snapshot_id'] != captured.snapshot_id:
            raise BusinessError('STATE_CONFLICT', 'Write proof refers to another observation', status=409)
        await surface.assert_current(captured.snapshot_id)
        snapshot = await asyncio.to_thread(surface._gateway.store.get_observation,
                                          token.run_id, captured.snapshot_id)
        if captured.source_url != snapshot['source_url']:
            raise BusinessError('STATE_CONFLICT', 'Write proof source differs from the actual page', status=409)
        check_id = check_id or 'write-check-' + uuid4().hex
        artifact = await asyncio.to_thread(self.evidence.store.publish,
            token.run_id, captured.data, source_url=captured.source_url, captured_at=utc_text(),
            object_id=operation_id, query_scope='external write result verification',
            locator_or_page=check_id, snapshot_id=captured.snapshot_id,
            execution_token=token, retain=True)
        # A metadata receipt or Playwright completion never establishes truth.
        # The store reopens and verifies the original artifact and live binding.
        return await asyncio.to_thread(self.store.record_check, token, operation_id,
            check_id, facts, [artifact['evidence_id']])
