"""Authenticated, bounded graph progress reads; no invocation authority."""
import re
import json
from fastapi import APIRouter, Request
from ..errors import BusinessError
from ..db import connect
from .store import GraphStore

router = APIRouter(prefix='/v1/runs')


def _cursor(request, run_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', run_id):
        raise BusinessError('INVALID_PARAMETER', 'Invalid Run identifier')
    values = request.query_params
    if (set(values) - {'after', 'limit'} or len(values.multi_items()) != len(values)
            or any(not re.fullmatch(r'0|[1-9][0-9]{0,18}', v) for v in values.values())):
        raise BusinessError('INVALID_PARAMETER', 'Invalid progress cursor')
    after, limit = int(values.get('after', '0')), int(values.get('limit', '100'))
    if not 0 <= after < 2**63 - 1 or not 1 <= limit <= 1000:
        raise BusinessError('INVALID_PARAMETER', 'Invalid progress cursor or page size')
    return after, limit


@router.get('/{run_id}/progress')
def progress(request: Request, run_id: str):
    after, limit = _cursor(request, run_id)
    store = GraphStore(request.app.state.settings.business_db)
    rows = store.list_progress(run_id, after=after, limit=limit)
    with connect(store.path) as db:
        waiting = db.execute('''SELECT i.wait_id,i.requested_fields_json FROM graph_input_requests i
            JOIN runs r USING(run_id) WHERE i.run_id=? AND r.state='PAUSED'
            AND i.state_version=r.state_version''',(run_id,)).fetchone()
    # All values are bounded IDs/enums/counters. Do not return file paths,
    # provider messages, prompts or framework saver implementation details.
    return {'run_id': run_id, 'progress': rows,
            'input_request': {'wait_id':waiting['wait_id'], 'requested_fields':json.loads(waiting['requested_fields_json'])}
                if waiting else None,
            'next_after': rows[-1]['progress_id'] if rows else after}


@router.get('/{run_id}/recovery')
def recovery(request: Request, run_id: str):
    """Report safe diagnostics even when the Run's graph version is blocked."""
    after, limit = _cursor(request, run_id)
    with connect(request.app.state.settings.business_db) as db:
        db.execute('BEGIN')
        run = db.execute('SELECT state,state_version FROM runs WHERE run_id=?', (run_id,)).fetchone()
        if run is None:
            raise BusinessError('NOT_FOUND', 'Run not found', status=404)
        rows = [dict(row) for row in db.execute('''SELECT recovery_seq,recovery_id,run_id,epoch,
            state_version,business_event_id,phase,reason,created_at FROM graph_recoveries
            WHERE run_id=? AND recovery_seq>? ORDER BY recovery_seq LIMIT ?''', (run_id,after,limit))]
    # Proof documents, account identifiers and live qualification never leave
    # this diagnostic endpoint. It cannot clear a block or invoke a graph.
    return {'run_id': run_id, 'state': run['state'], 'state_version': run['state_version'],
            'recoveries': rows, 'next_after': rows[-1]['recovery_seq'] if rows else after}
