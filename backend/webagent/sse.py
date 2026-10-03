"""Durable SSE feed. Poll SQLite in short reads; never queue unbounded events."""
import asyncio
import time

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from .db.repository import canonical_json
from .events import read_events, validate_cursor

router = APIRouter()
POLL_SECONDS = 0.2
HEARTBEAT_SECONDS = 15.0
PAGE_SIZE = 100


def encode_event(event: dict) -> str:
    return f'id: {event["event_id"]}\nevent: {event["event_type"]}\ndata: {canonical_json(event)}\n\n'


@router.get('/v1/events')
async def events(request: Request,
                 last_event_id: str | None = Header(default=None, alias='Last-Event-ID'),
                 task_id: str | None = Query(default=None, min_length=1, max_length=200),
                 run_id: str | None = Query(default=None, min_length=1, max_length=200)):
    path = request.app.state.settings.business_db
    cursor = await run_in_threadpool(validate_cursor, path, last_event_id)
    # Read before HTTP headers so startup storage failures can return JSON errors.
    initial = await run_in_threadpool(read_events, path, after=cursor, task_id=task_id,
                                     run_id=run_id, limit=PAGE_SIZE)

    async def stream():
        nonlocal cursor
        yield 'retry: 1000\n\n'
        page = initial
        last_sent = time.monotonic()
        while not await request.is_disconnected():
            for event in page:
                yield encode_event(event)
                cursor = event['event_id']
                last_sent = time.monotonic()
            if len(page) < PAGE_SIZE:
                if time.monotonic() - last_sent >= HEARTBEAT_SECONDS:
                    yield ': keep-alive\n\n'
                    last_sent = time.monotonic()
                await asyncio.sleep(POLL_SECONDS)
            page = await run_in_threadpool(read_events, path, after=cursor, task_id=task_id,
                                           run_id=run_id, limit=PAGE_SIZE)

    return StreamingResponse(stream(), media_type='text/event-stream', headers={
        'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no',
    })
