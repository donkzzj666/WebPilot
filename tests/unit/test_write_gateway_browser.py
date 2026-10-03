"""The private write-check surface remains read-only despite old allowances."""
import asyncio
from dataclasses import asdict, replace

import pytest

from webagent.errors import BusinessError
from unit.test_gateway_browser import backend, TOKEN, URL, action


def test_write_check_context_allows_only_fixed_capture_get_and_no_prepared_action():
    async def exercise():
        browser, page, context, managed = backend()
        async def ordinary_denied(*args, **kwargs):
            raise BusinessError('RESOURCE_CONFLICT', 'Ordinary execution is fenced', status=409)
        async def checked_context(session_id, owner, *, execution_token, operation_id):
            if execution_token != TOKEN or operation_id != 'synthetic-operation':
                raise BusinessError('RESOURCE_CONFLICT', 'Write query binding changed', status=409)
            return context
        managed.context, managed.write_check_context = ordinary_denied, checked_context
        browser._allowed_mutations = frozenset({('POST', URL)})
        captured = await browser.write_check_capture(TOKEN, 'synthetic-operation')
        assert captured.page_url == URL and not page.main_frame.calls
        browser._write_check_token, browser._write_check_operation_id = TOKEN, 'synthetic-operation'
        try:
            assert await browser._request_permitted('GET', URL)
            assert await browser._request_permitted('HEAD', URL)
            for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
                assert not await browser._request_permitted(method, URL)
            snapshot = {**asdict(captured), 'snapshot_id': 'query-snapshot', 'source_url': URL}
            with pytest.raises(BusinessError):
                await browser.prepare(TOKEN, action(snapshot), snapshot)
            with pytest.raises(BusinessError):
                await browser.write_check_capture(TOKEN, 'synthetic-operation')
        finally:
            browser._write_check_token = browser._write_check_operation_id = None
        result = await browser.write_check_navigate(TOKEN, 'https://fixture.example/result', 'synthetic-operation')
        assert result['source_url'].endswith('/result')
        assert [call[0] for call in page.main_frame.calls] == ['goto']
        assert browser._write_check_token is None and browser._write_check_operation_id is None
        await browser.aclose()
    asyncio.run(exercise())


def test_write_check_stale_binding_or_unleased_get_never_touches_page():
    async def exercise():
        browser, page, context, managed = backend()
        async def checked_context(session_id, owner, *, execution_token, operation_id):
            if execution_token != TOKEN or operation_id != 'synthetic-operation':
                raise BusinessError('RESOURCE_CONFLICT', 'Write query binding changed', status=409)
            return context
        managed.write_check_context = checked_context
        for token, operation_id in ((replace(TOKEN, epoch=2), 'synthetic-operation'), (TOKEN, 'another-operation')):
            with pytest.raises(BusinessError):
                await browser.write_check_capture(token, operation_id)
        with pytest.raises(BusinessError):
            await browser.write_check_navigate(TOKEN, 'https://other.example/result', 'synthetic-operation')
        assert not page.main_frame.calls
        assert browser._write_check_token is None
        await browser.aclose()
    asyncio.run(exercise())
