"""Authenticated, read-only quota and Run-budget projections.

Reading these endpoints never opens a timer, settles an interval, consumes a
quota or starts a Run. The Worker owns every budget mutation.
"""
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request

from ..db import connect
from .models import MONITOR_SOURCES
from .store import BudgetStore

router = APIRouter(prefix='/v1/budgets')
SHANGHAI = ZoneInfo('Asia/Shanghai')


def current_quota(path: Path, *, now: datetime | None = None) -> dict:
    """Read one SQLite snapshot for the current Shanghai quota date."""
    now = now or datetime.now(timezone.utc)
    date = now.astimezone(SHANGHAI).date().isoformat()
    with connect(path) as db:
        # A deferred read transaction supplies a consistent WAL snapshot and
        # does not take the writer lock used by dispatch reservations.
        db.execute('BEGIN')
        bucket = db.execute("SELECT capacity,used FROM quota_buckets WHERE quota_date=? AND quota_type='public'",
                            (date,)).fetchone()
        capacity, used = (min(50, bucket['capacity']), bucket['used']) if bucket else (50, 0)
        counts = {row['debit_kind']: row['used'] for row in db.execute('''
            SELECT debit_kind,count(*) AS used FROM quota_debits
            WHERE quota_date=? AND quota_type='public' GROUP BY debit_kind''', (date,))}
        by_source = {row['source_kind']: row['used'] for row in db.execute('''
            SELECT s.source_kind,count(*) AS used FROM quota_monitor_sources s
            JOIN quota_debits d USING(debit_id) WHERE d.quota_date=?
            AND d.quota_type='public' AND d.debit_kind='monitoring' GROUP BY s.source_kind''', (date,))}
    used = max(used, sum(counts.values()))
    remaining = max(0, capacity - used)
    ordinary = counts.get('ordinary', 0)
    monitoring_used = counts.get('monitoring', 0)
    unknown_source_used = max(0, monitoring_used - sum(by_source.values()))
    source_status = {source: {'capacity': 4, 'used': by_source.get(source, 0),
                             'remaining': min(remaining, max(0, 4 - by_source.get(source, 0) - unknown_source_used))}
                     for source in sorted(MONITOR_SOURCES)}
    return {
        'quota_date': date, 'timezone': 'Asia/Shanghai', 'scope': 'configured_data_directory',
        'public': {'capacity': capacity, 'used': used, 'remaining': remaining,
                   'ordinary': {'capacity': 42, 'used': ordinary,
                                'remaining': min(remaining, max(0, 42 - ordinary))},
                   'monitoring': {'reserved': 8, 'used': monitoring_used,
                                  'unknown_source_used': unknown_source_used,
                                  'remaining': min(remaining, max(0, 8 - monitoring_used),
                                                   sum(item['remaining'] for item in source_status.values())),
                                  'sources': source_status}},
        'webarena': {'uses_public_quota': False},
    }


@router.get('')
def quotas(request: Request):
    return current_quota(request.app.state.settings.business_db)


@router.get('/runs/{run_id}')
def run_budget(run_id: str, request: Request):
    return BudgetStore(request.app.state.settings.business_db).status(run_id)
