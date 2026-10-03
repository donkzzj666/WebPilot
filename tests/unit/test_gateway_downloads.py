"""Managed downloads require one exact private gateway permit and fresh leases.

All browser/download objects here are inert fakes. Lease and session validation
uses actual temporary SQLite; no user browser, credential or network is used.
"""
import asyncio
from dataclasses import replace

import pytest

from test_session_manager import OWNER, Page, setup
from webagent.db import StorageBusyError, connect, transaction
from webagent.db.repository import add_contract, create_run, create_task, utc_text
from webagent.errors import BusinessError
from webagent.scheduler.models import Resource
from webagent.scheduler.store import SchedulerStore
from webagent.sessions.models import SessionOwner
from webagent.tasks.compiler import compile_draft

URL = 'http://fixture.example/fixture/attachment.pdf'


class Download:
    def __init__(self, url=URL):
        self.url, self.cancelled = url, False

    async def cancel(self):
        self.cancelled = True


async def scheduled(tmp_path, *, optin=True):
    manager, factory, auth = setup(tmp_path)
    await manager.start()
    now = utc_text()
    contract = compile_draft(
        {'instruction': 'Synthetic managed gateway download', 'scenario': 'research',
         'source_ids': ['local-fixture'], 'parameters': {'queries': ['fixture'],
          'topic_criteria': ['fixture'], 'cutoff_at': now, 'max_items': 3}},
        task_id='download-task', version=1, created_at=now,
        provenance=[{'origin': 'api', 'reference': 'download-fixture',
                     'content_sha256': 'a' * 64, 'authorizes_execution': True}],
    ).contract
    with connect(manager.settings.business_db) as db, transaction(db):
        create_task(db, task_id='download-task', instruction=contract['original_instruction'],
                    requested_fields=['contract'])
        add_contract(db, contract)
        create_run(db, run_id='download-run', task_id='download-task', contract_version=1,
                   graph_version='gateway-test', graph_state_schema_version='gateway-v1',
                   model_config_sha256='a' * 64, runtime_config_sha256='b' * 64)
    scheduler = SchedulerStore(manager.settings.business_db)
    generation = scheduler.start_worker('download-worker')
    scheduler.enqueue('download-run', [Resource.site_identity('local-fixture', realm='webarena'),
                                       Resource.browser_context('download-run')],
                      expected_state_version=0, queue_class='webarena')
    token = scheduler.claim('download-worker', generation)
    owner = SessionOwner('run', 'download-run', 'local-fixture', realm='webarena')
    session = await manager.create(owner, execution_token=token, gateway_downloads=optin)
    context = await manager.context(session.session_id, owner, execution_token=token)
    return manager, factory, auth, scheduler, token, owner, session, context, context.pages[0]


@pytest.mark.parametrize('owner', [OWNER, SessionOwner('login', 'synthetic-login', 'fixture')])
def test_download_optin_is_not_available_to_login_or_verification_contexts(tmp_path, owner):
    async def exercise():
        manager, factory, _ = setup(tmp_path)
        try:
            with pytest.raises(BusinessError) as denied:
                await manager.create(owner, gateway_downloads=True, execution_token=object())
            assert denied.value.code == 'FORBIDDEN'
            assert not factory.starts and not manager._started
        finally:
            await manager.aclose()
    asyncio.run(exercise())


def test_download_optin_requires_current_scheduled_run_token(tmp_path):
    async def exercise():
        manager, factory, _ = setup(tmp_path)
        owner = SessionOwner('run', 'missing-run', 'fixture')
        try:
            with pytest.raises(BusinessError) as denied:
                await manager.create(owner, gateway_downloads=True)
            assert denied.value.code == 'FORBIDDEN' and not factory.starts
        finally:
            await manager.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize('value', [None, 0, 1, 'true', [], {}])
def test_download_optin_must_be_a_real_boolean(tmp_path, value):
    async def exercise():
        manager, factory, _ = setup(tmp_path)
        try:
            with pytest.raises(BusinessError) as denied:
                await manager.create(OWNER, gateway_downloads=value)
            assert denied.value.code == 'INVALID_PARAMETER' and denied.value.field == 'gateway_downloads'
            assert not factory.starts and not manager._started
        finally:
            await manager.aclose()
    asyncio.run(exercise())


def test_default_browser_context_rejects_downloads_and_cancels_unsolicited_events(tmp_path):
    async def exercise():
        manager, factory, _ = setup(tmp_path)
        try:
            session = await manager.create(OWNER)
            context = await manager.context(session.session_id, OWNER)
            assert factory.created_contexts[0].options['accept_downloads'] is False
            download = Download()
            context.pages[0].emit('download', download)
            await manager.drain_events()
            assert download.cancelled
        finally:
            await manager.aclose()
    asyncio.run(exercise())


def test_scheduled_optin_context_still_cancels_every_download_without_permission(tmp_path):
    async def exercise():
        manager, factory, _, _, _, _, _, _, page = await scheduled(tmp_path)
        try:
            assert factory.created_contexts[0].options['accept_downloads'] is True
            download = Download()
            page.emit('download', download)
            await manager.drain_events()
            assert download.cancelled
        finally:
            await manager.aclose()
    asyncio.run(exercise())


def test_fresh_run_without_optin_cannot_obtain_download_permission(tmp_path):
    async def exercise():
        manager, _, _, _, token, owner, session, _, page = await scheduled(tmp_path, optin=False)
        try:
            with pytest.raises(BusinessError) as denied:
                async with manager.download_permission(session.session_id, owner,
                        execution_token=token, page=page, attachment_url=URL):
                    pytest.fail('Disabled context was granted download permission')
            assert denied.value.code == 'FORBIDDEN'
        finally:
            await manager.aclose()
    asyncio.run(exercise())


def test_exact_page_and_url_permission_is_consumed_once_before_async_checks(tmp_path):
    async def exercise():
        manager, _, _, _, token, owner, session, context, page = await scheduled(tmp_path)
        try:
            other_page = await context.new_page()
            wrong_page, wrong_url, first, second = Download(), Download(URL + '?other'), Download(), Download()
            async with manager.download_permission(session.session_id, owner,
                    execution_token=token, page=page, attachment_url=URL):
                other_page.emit('download', wrong_page)
                page.emit('download', wrong_url)
                page.emit('download', first)
                page.emit('download', second)
                await manager.drain_events()
            assert wrong_page.cancelled and wrong_url.cancelled and second.cancelled
            assert not first.cancelled
            assert manager._contexts[session.session_id].download_permit is None
        finally:
            await manager.aclose()
    asyncio.run(exercise())


def test_epoch_revoked_after_event_before_validation_cancels_once_permitted_download(tmp_path):
    async def exercise():
        manager, _, _, scheduler, token, owner, session, _, page = await scheduled(tmp_path)
        try:
            download = Download()
            async with manager.download_permission(session.session_id, owner,
                    execution_token=token, page=page, attachment_url=URL):
                page.emit('download', download)
                scheduler.defer(token, 'PAUSED')
                await manager.drain_events()
            assert download.cancelled
            assert manager._contexts[session.session_id].download_permit is None
        finally:
            await manager.aclose()
    asyncio.run(exercise())


def test_download_qualification_lookup_failure_cancels_instead_of_accepting_uncertain_download(tmp_path, monkeypatch):
    async def exercise():
        manager, factory, _, _, token, owner, session, _, page = await scheduled(tmp_path)
        try:
            download = Download()
            async with manager.download_permission(session.session_id, owner,
                    execution_token=token, page=page, attachment_url=URL):
                def busy(*args, **kwargs):
                    raise StorageBusyError('Synthetic authority is temporarily unavailable')
                monkeypatch.setattr(manager.registry, 'validate_execution', busy)
                page.emit('download', download)
                try:
                    await manager.drain_events()
                except BusinessError:
                    pass  # It may additionally disable further manager work.
            assert download.cancelled
            assert manager._event_failed
        finally:
            monkeypatch.undo()
            with pytest.raises(BusinessError) as failed_closed:
                await manager.aclose()
            assert failed_closed.value.code == 'SERVICE_UNAVAILABLE' and failed_closed.value.status == 503
            assert manager._lock_fd is None and manager._contexts == {}
            assert all(context.closed for context in factory.created_contexts)
            assert all(not browser.is_connected() for browser in factory.browsers)
    asyncio.run(exercise())


def test_permission_exit_on_exception_clears_authority_for_late_download(tmp_path):
    async def exercise():
        manager, _, _, _, token, owner, session, _, page = await scheduled(tmp_path)
        try:
            with pytest.raises(RuntimeError, match='synthetic operation failed'):
                async with manager.download_permission(session.session_id, owner,
                        execution_token=token, page=page, attachment_url=URL):
                    raise RuntimeError('synthetic operation failed')
            assert manager._contexts[session.session_id].download_permit is None
            late = Download()
            page.emit('download', late)
            await manager.drain_events()
            assert late.cancelled
            expected = Download()
            async with manager.download_permission(session.session_id, owner,
                    execution_token=token, page=page, attachment_url=URL):
                page.emit('download', expected)
                await manager.drain_events()
            assert not expected.cancelled
        finally:
            await manager.aclose()
    asyncio.run(exercise())


def test_foreign_or_closed_page_cannot_receive_permission(tmp_path):
    async def exercise():
        manager, _, _, _, token, owner, session, context, page = await scheduled(tmp_path)
        try:
            foreign_page = Page(object())
            closed_page = await context.new_page()
            await closed_page.close()
            for candidate in (foreign_page, closed_page):
                with pytest.raises(BusinessError) as denied:
                    async with manager.download_permission(session.session_id, owner,
                            execution_token=token, page=candidate, attachment_url=URL):
                        pytest.fail('Foreign or closed page received download permission')
                assert denied.value.code == 'FORBIDDEN'
            assert not page.is_closed()
        finally:
            await manager.aclose()
    asyncio.run(exercise())


def test_stale_token_and_nested_permission_do_not_replace_existing_grant(tmp_path):
    async def exercise():
        manager, _, _, _, token, owner, session, _, page = await scheduled(tmp_path)
        try:
            with pytest.raises(BusinessError):
                async with manager.download_permission(session.session_id, owner,
                        execution_token=replace(token, epoch=token.epoch + 1), page=page, attachment_url=URL):
                    pytest.fail('Stale token received download permission')
            async with manager.download_permission(session.session_id, owner,
                    execution_token=token, page=page, attachment_url=URL):
                with pytest.raises(BusinessError) as denied:
                    async with manager.download_permission(session.session_id, owner,
                            execution_token=token, page=page, attachment_url=URL + '?second'):
                        pytest.fail('Nested permission overwrote the original permit')
                assert denied.value.code == 'FORBIDDEN'
                expected = Download()
                page.emit('download', expected)
                await manager.drain_events()
                assert not expected.cancelled
        finally:
            await manager.aclose()
    asyncio.run(exercise())


def test_gateway_proxy_auth_returns_only_current_run_owned_proxy_and_never_journals_credentials(tmp_path):
    async def exercise():
        manager, _, _, _, token, owner, session, _, _ = await scheduled(tmp_path)
        try:
            credentials = await manager.gateway_proxy_credentials(session.session_id, owner,
                                                                  execution_token=token)
            expected = manager._contexts[session.session_id].proxy.playwright_proxy
            assert set(credentials) == {'server', 'username', 'password'}
            assert all(credentials[key] == expected[key] for key in credentials)
            credentials['username'] = 'synthetic-caller-change'
            current = await manager.gateway_proxy_credentials(session.session_id, owner,
                                                              execution_token=token)
            assert all(current[key] == expected[key] for key in current)
            with connect(manager.settings.business_db) as db:
                durable = '\n'.join(db.iterdump())
            assert all(current[key] not in durable for key in ('username', 'password'))
        finally:
            await manager.aclose()
    asyncio.run(exercise())


def test_gateway_proxy_auth_rejects_stale_epoch_before_returning_any_credentials(tmp_path):
    async def exercise():
        manager, _, _, _, token, owner, session, _, _ = await scheduled(tmp_path)
        try:
            with pytest.raises(BusinessError) as stale:
                await manager.gateway_proxy_credentials(session.session_id, owner,
                    execution_token=replace(token, epoch=token.epoch + 1))
            assert stale.value.code == 'RESOURCE_CONFLICT' and stale.value.status == 409
        finally:
            await manager.aclose()
    asyncio.run(exercise())


def test_gateway_proxy_auth_is_unavailable_to_login_and_verification_sessions(tmp_path):
    async def exercise():
        manager, _, _, _, token, _, _, _, _ = await scheduled(tmp_path)
        try:
            for kind in ('login', 'verification'):
                owner = SessionOwner(kind, 'proxy-' + kind, 'other-fixture')
                session = await manager.create(owner)
                with pytest.raises(BusinessError) as denied:
                    await manager.gateway_proxy_credentials(session.session_id, owner, execution_token=token)
                assert denied.value.code == 'FORBIDDEN' and denied.value.status == 403
        finally:
            await manager.aclose()
    asyncio.run(exercise())
