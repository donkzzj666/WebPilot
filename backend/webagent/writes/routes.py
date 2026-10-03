"""Authenticated diagnostics; no adapter installation or write invocation."""
from fastapi import APIRouter, Request

from ..db import connect
from ..errors import BusinessError
from ..graph.routes import _cursor

router = APIRouter(prefix='/v1/runs')


@router.get('/{run_id}/write-intents')
def write_intents(request: Request, run_id: str):
    after, limit = _cursor(request, run_id)
    with connect(request.app.state.settings.business_db) as db:
        db.execute('BEGIN')
        run = db.execute('SELECT task_id FROM runs WHERE run_id=?', (run_id,)).fetchone()
        if run is None:
            raise BusinessError('NOT_FOUND', 'Run not found', status=404)
        rows = [dict(row) for row in db.execute('''SELECT w.rowid AS intent_seq,
            w.operation_id,w.originating_run_id,w.status,
            (w.receipt IS NOT NULL) AS receipt_available,
            (SELECT count(*) FROM write_intent_attempts a WHERE a.operation_id=w.operation_id) AS attempts,
            (SELECT count(*) FROM write_protocol_checks c WHERE c.operation_id=w.operation_id) AS checks
            FROM write_intents w WHERE w.task_id=? AND w.rowid>? ORDER BY w.rowid LIMIT ?''',
            (run['task_id'], after, limit))]
        pending = db.execute("SELECT count(*) FROM write_intents WHERE task_id=? AND status IN ('INTENT','UNKNOWN')",
                             (run['task_id'],)).fetchone()[0]
    # The receipt body, semantic claim, proof bytes, paths and live token stay
    # behind the Worker/evidence boundary. This endpoint cannot clear UNKNOWN.
    return {'run_id': run_id, 'write_intents': rows, 'pending_count': pending,
            'next_after': rows[-1]['intent_seq'] if rows else after}
