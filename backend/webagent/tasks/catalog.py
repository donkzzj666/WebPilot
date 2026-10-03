"""Bounded, read-only task catalog for the local task entry page.

The cursor follows immutable insertion order, so later inserts cannot move a
task into an already-read page. It is unrelated to business event order.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from ..db.repository import utc_text
from ..errors import BusinessError

MAX_PAGE = 100
MAX_CURSOR = 2**63 - 1


def list_tasks(path: Path, *, before: int | None = None, limit: int = 20) -> dict:
    if (type(limit) is not int or not 1 <= limit <= MAX_PAGE
            or before is not None and (type(before) is not int or not 1 <= before <= MAX_CURSOR)):
        raise BusinessError('INVALID_PARAMETER', '任务列表分页参数无效。')
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True,
                                     isolation_level=None, timeout=.1)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            remaining = 5000
            def bounded():
                nonlocal remaining
                remaining -= 1
                return int(remaining < 0)
            db.set_progress_handler(bounded, 1000)
            db.execute('BEGIN')
            as_of = utc_text()
            where = '' if before is None else 'WHERE t.rowid < ?'
            values = (limit + 1,) if before is None else (before, limit + 1)
            rows = db.execute('''SELECT t.rowid AS catalog_cursor,t.task_id,t.original_instruction,
                t.preparation_status,t.current_contract_version,t.current_run_id,t.state_version,t.created_at,
                t.requested_fields_json,r.state AS current_run_state,
                COALESCE((SELECT created_at FROM task_revisions v WHERE v.task_id=t.task_id
                    ORDER BY revision DESC LIMIT 1),t.created_at) AS updated_at,
                (SELECT revision FROM task_revisions v WHERE v.task_id=t.task_id
                    ORDER BY revision DESC LIMIT 1) AS revision
                FROM tasks t LEFT JOIN runs r ON r.run_id=t.current_run_id AND r.task_id=t.task_id
                ''' + where + ' ORDER BY t.rowid DESC LIMIT ?', values).fetchall()
            result = []
            for row in rows[:limit]:
                item = dict(row)
                item.pop('catalog_cursor')
                fields = json.loads(item.pop('requested_fields_json'))
                if not isinstance(fields, list) or any(type(field) is not str for field in fields):
                    raise ValueError('invalid_requested_fields')
                item['requested_fields'] = fields
                result.append(item)
            return {'tasks': result,
                    # Decimal text avoids losing a SQLite rowid in browser JS.
                    'next_cursor': str(rows[limit - 1]['catalog_cursor']) if len(rows) > limit else None,
                    'as_of': as_of}
    except (sqlite3.Error, ValueError, TypeError, OSError):
        raise BusinessError('STORAGE_UNAVAILABLE', '暂时无法读取任务列表，请稍后刷新。', status=503) from None
