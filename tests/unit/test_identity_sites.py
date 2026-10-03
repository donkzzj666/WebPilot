"""Fixed identity signals, safe failures and changing-navigation regression cases."""
import asyncio
from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
from types import SimpleNamespace

import pytest

from webagent.errors import BusinessError
from webagent.identities.sites import (FixtureSiteAdapter, GitHubSiteAdapter, IdentityReason,
    IdentityStatus, SiteAdapter, SiteCatalog, _READ_SIGNALS)


URL = 'https://github.com/settings/profile'


def signals(account='Alice', **updates):
    return {'url': URL, 'ready': 'complete', 'login': {'count': 1, 'values': [account]},
        'hostname': {'count': 1, 'values': ['github.com']},
        'expectedHost': {'count': 1, 'values': ['github.com']},
        'loggedIn': True, 'loggedOut': False, 'loginForm': False, **updates}


class CDP:
    def __init__(self, page):
        self.page = page
        self.calls = []
        self.reads = 0
        self.frames = 0
        self.detached = False

    async def send(self, method, params=None):
        self.calls.append((method, params))
        if self.page.failure == method:
            raise RuntimeError('SYNTHETIC_SECRET_MUST_NOT_APPEAR')
        if method == 'Page.getFrameTree':
            self.frames += 1
            return {'frameTree': {'frame': {'id': 'main-frame',
                'loaderId': 'loader-2' if self.page.loader_changed and self.frames == 2 else 'loader-1',
                'url': self.page.url}}}
        if method == 'Page.createIsolatedWorld':
            assert params == {'frameId': 'main-frame', 'worldName': 'webpilot-identity-confirmation'}
            return {'executionContextId': 123}
        if method == 'Runtime.evaluate':
            assert params['expression'] == _READ_SIGNALS
            assert params['contextId'] == 123 and params['returnByValue'] is True
            self.reads += 1
            if self.reads == 2:
                if self.page.during_read:
                    self.page.during_read(self.page)
                if self.page.second is not None:
                    return {'result': {'value': self.page.second}}
            return {'result': {'value': deepcopy(self.page.signals)}}
        raise AssertionError('Unexpected browser protocol command')

    async def detach(self):
        self.detached = True


class Page:
    def __init__(self, *, account='Alice', final_url=URL, status=200, redirected=False,
                 content_type='text/html; charset=utf-8', sw=False, failure=None, **signal_updates):
        self.url = 'https://github.com/login'
        self.final_url = final_url
        self.signals = signals(account, **signal_updates)
        self.main_frame = SimpleNamespace(url=self.url)
        self.handlers = {}
        self.status = status
        self.redirected = redirected
        self.content_type = content_type
        self.sw = sw
        self.failure = failure
        self.closed = False
        self.loader_changed = False
        self.during_read = None
        self.second = None
        self.goto_calls = []
        self.cdp = CDP(self)
        self.context = SimpleNamespace(new_cdp_session=self.new_cdp_session)

    def is_closed(self):
        return self.closed

    def on(self, event, callback):
        self.handlers.setdefault(event, []).append(callback)

    def remove_listener(self, event, callback):
        self.handlers[event].remove(callback)

    def emit(self, event, value=None):
        for callback in self.handlers.get(event, []):
            callback(value)

    def request(self, url):
        return SimpleNamespace(url=url, method='GET', frame=self.main_frame,
                               is_navigation_request=lambda: True,
                               redirected_from=object() if self.redirected else None)

    async def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))
        if self.failure == 'goto':
            raise RuntimeError('SYNTHETIC_SECRET_MUST_NOT_APPEAR')
        if self.failure == 'timeout':
            raise TimeoutError('SYNTHETIC_SECRET_MUST_NOT_APPEAR')
        if self.failure == 'cancelled':
            raise asyncio.CancelledError()
        self.url = self.final_url
        request = self.request(url)
        self.emit('request', request)
        self.main_frame.url = self.final_url
        self.emit('framenavigated', self.main_frame)
        if self.failure == 'no-response':
            return None
        return SimpleNamespace(url=self.url, request=request, status=self.status,
                               from_service_worker=self.sw, header_value=self.header_value)

    async def header_value(self, name):
        assert name == 'content-type'  # No Cookie/Set-Cookie/all_headers access.
        return self.content_type

    async def new_cdp_session(self, page):
        assert page is self
        return self.cdp


def confirm(page, expected='alice'):
    observation = asyncio.run(GitHubSiteAdapter().confirm(page, expected))
    assert not any(page.handlers.values()), 'Confirmation listeners leaked'
    if page.cdp.calls:
        assert page.cdp.detached
    return observation


def test_default_catalog_exposes_only_fixed_github_urls_and_is_immutable():
    catalog = SiteCatalog()
    adapter = catalog.get('github')
    assert adapter.origin == 'https://github.com' and adapter.adapter_id == 'github-auth-v1'
    assert adapter.realm == 'public' and adapter.verification_url == URL
    assert adapter.login_url == 'https://github.com/login?return_to=%2Fsettings%2Fprofile'
    assert [item['site_id'] for item in catalog.list()] == ['github']
    listing = catalog.list(); listing[0]['login_url'] = 'https://evil.invalid/'
    assert catalog.get('github').login_url != listing[0]['login_url']
    with pytest.raises(FrozenInstanceError):
        adapter.login_url = 'https://evil.invalid/'


@pytest.mark.parametrize('site', ['gitlab', 'https://github.com', 'GITHUB', '../github', '', None, []])
def test_unregistered_site_never_becomes_arbitrary_navigation(site):
    with pytest.raises(BusinessError) as caught:
        SiteCatalog().get(site)
    assert caught.value.code == 'INVALID_PARAMETER' and caught.value.field == 'site_id'


@pytest.mark.parametrize('updates', [{'site_id': 'evil'}, {'realm': 'webarena'},
    {'login_url': 'https://evil.invalid/login'}, {'verification_url': 'https://github.com/alice'}])
def test_production_catalog_configuration_cannot_override_urls(updates):
    with pytest.raises(ValueError):
        SiteAdapter(**updates)


def test_fixture_catalog_requires_explicit_python_injection_and_fixed_signals():
    adapter = FixtureSiteAdapter(site_id='fixture-github', origin='http://127.0.0.1:23456')
    catalog = SiteCatalog(adapters=(adapter,))
    assert adapter.realm == 'webarena' and adapter.adapter_id == 'fixture-github-auth-v1'
    assert adapter.login_url == 'http://127.0.0.1:23456/login'
    assert adapter.verification_url == 'http://127.0.0.1:23456/identity'
    assert catalog.get('fixture-github') is adapter
    with pytest.raises(BusinessError):
        SiteCatalog().get('fixture-github')
    with pytest.raises(ValueError):
        SiteCatalog((adapter, adapter))


@pytest.mark.parametrize('origin', ['https://github.com', 'http://evil.invalid:8000',
    'http://localhost:8000', 'http://user:pass@127.0.0.1:8000', 'http://127.0.0.1:8000/path',
    'http://127.0.0.1:8000?query', 'http://127.0.0.1:8000#fragment', 'http://127.0.0.1:1'])
def test_fixture_origin_is_still_strict_loopback(origin):
    with pytest.raises(ValueError):
        FixtureSiteAdapter(site_id='fixture', origin=origin)


@pytest.mark.parametrize('value', ['', ' Alice', 'Alice ', '@alice', 'alice@example.com', 'https://github.com/alice',
    'a--b', '-alice', 'alice-', 'аlice', 'alice\n', 'a' * 40, None, 123])
def test_account_normalization_rejects_ambiguous_nonlogin_values(value):
    with pytest.raises(BusinessError) as caught:
        GitHubSiteAdapter().normalize_account(value)
    assert caught.value.field == 'expected_account'


def test_verified_uses_only_exact_normalized_login_and_stable_safe_evidence():
    first = confirm(Page(account='Alice'), 'ALICE')
    second_page = Page(account='alice'); second = confirm(second_page)
    assert first.status == IdentityStatus.VERIFIED and first.account == first.normalized_account == 'alice'
    assert first.reason == IdentityReason.VERIFIED and first.verification_url == URL
    assert len(first.evidence_sha256) == len(first.document_token) == 64
    assert first.evidence_sha256 == second.evidence_sha256
    assert second_page.goto_calls == [(URL, {'wait_until': 'load', 'timeout': 5000})]
    assert second_page.cdp.reads == 2


def test_wrong_account_is_not_a_verified_identity():
    observed = confirm(Page(account='malice'), 'alice')
    assert observed.status == IdentityStatus.ACCOUNT_MISMATCH
    assert observed.account == 'malice'


@pytest.mark.parametrize('url', ['https://evil.invalid/settings/profile', 'http://github.com/settings/profile',
    'https://github.com.attacker.invalid/settings/profile', 'https://github.com/alice',
    'https://github.com/settings/profile/', 'https://github.com/settings/profile?x=1',
    'https://github.com/settings/profile#alice', 'https://github.com/settings/%70rofile',
    'https://github.com/settings/a/../profile', 'about:blank'])
def test_wrong_origin_path_encoding_query_or_fragment_never_counts_as_identity(url):
    observed = confirm(Page(final_url=url))
    assert observed.status == IdentityStatus.UNVERIFIABLE and observed.account is None
    assert observed.verification_url is None and observed.evidence_sha256 is None


def test_login_redirect_is_not_authenticated_and_does_not_read_account_signals():
    page = Page(final_url='https://github.com/login?return_to=%2Fsettings%2Fprofile', redirected=True)
    observed = confirm(page)
    assert observed.status == IdentityStatus.NOT_AUTHENTICATED and page.cdp.reads == 0


def test_redirect_chain_ending_at_expected_page_is_still_rejected():
    observed = confirm(Page(redirected=True))
    assert observed.reason == IdentityReason.REDIRECTED


@pytest.mark.parametrize('status', [201, 204, 301, 304, 404, 500])
def test_unexpected_document_response_rejected(status):
    assert confirm(Page(status=status)).reason == IdentityReason.RESPONSE_REJECTED


@pytest.mark.parametrize('status', [401, 403])
def test_auth_rejection_response_never_reads_meta(status):
    page = Page(status=status)
    assert confirm(page).status == IdentityStatus.NOT_AUTHENTICATED and page.cdp.reads == 0


@pytest.mark.parametrize('updates', [{'sw': True}, {'content_type': 'application/json'}, {'content_type': None}])
def test_service_worker_or_non_html_document_is_unverifiable(updates):
    assert confirm(Page(**updates)).reason == IdentityReason.RESPONSE_REJECTED


@pytest.mark.parametrize('updates', [
    {'login': {'count': 0, 'values': []}},
    {'login': {'count': 2, 'values': ['alice', 'bob']}},
    {'login': {'count': 2, 'values': ['alice', 'alice']}},
    {'login': {'count': 1, 'values': ['alice'], 'extra': 'bad'}},
    {'hostname': {'count': 1, 'values': ['evil.invalid']}},
    {'expectedHost': {'count': 1, 'values': ['evil.invalid']}},
    {'loggedIn': True, 'loggedOut': True}, {'loggedIn': True, 'loginForm': True},
    {'ready': 'loading'}, {'loggedIn': 'true'},
])
def test_missing_conflicting_or_changing_signals_never_verify(updates):
    assert confirm(Page(**updates)).status == IdentityStatus.UNVERIFIABLE


@pytest.mark.parametrize('account', ['', 'alice secret', '<script>', None, 'x' * 10000])
def test_fake_login_values_are_not_persisted_or_returned(account):
    observed = confirm(Page(account=account))
    assert observed.reason == IdentityReason.INVALID_ACCOUNT_SIGNAL
    assert observed.account is None and observed.evidence_sha256 is None


@pytest.mark.parametrize('updates', [{'loggedIn': False, 'loggedOut': True}, {'loggedIn': False}])
def test_username_meta_alone_is_not_authenticated(updates):
    assert confirm(Page(**updates)).status == IdentityStatus.NOT_AUTHENTICATED


def test_inflight_navigation_request_is_denied_even_before_url_commit():
    page = Page()
    page.during_read = lambda page: page.emit('request', page.request('https://github.com/login'))
    assert confirm(page).reason == IdentityReason.NAVIGATION_CHANGED


def test_same_url_reload_is_denied_even_if_signals_match():
    page = Page(); page.loader_changed = True
    assert confirm(page).reason == IdentityReason.NAVIGATION_CHANGED


def test_account_changed_between_reads_is_denied():
    page = Page(); page.second = signals('bob')
    assert confirm(page).reason == IdentityReason.SIGNAL_CONFLICT


def test_navigation_away_and_back_cannot_reuse_previous_identity():
    page = Page()
    page.during_read = lambda page: page.emit('framenavigated', page.main_frame)
    assert confirm(page).reason == IdentityReason.NAVIGATION_CHANGED


def test_closed_or_crashed_page_never_verifies():
    page = Page(); page.during_read = lambda page: page.emit('crash')
    assert confirm(page).reason == IdentityReason.NAVIGATION_CHANGED
    page = Page(); page.closed = True
    assert confirm(page).reason == IdentityReason.PAGE_UNAVAILABLE


@pytest.mark.parametrize('failure', ['goto', 'no-response', 'Page.getFrameTree', 'Runtime.evaluate', 'timeout'])
def test_safe_errors_never_contain_browser_error_or_page_data(failure):
    observed = confirm(Page(failure=failure))
    assert observed.status == IdentityStatus.UNVERIFIABLE
    assert 'SYNTHETIC_SECRET' not in repr(observed)


def test_cancellation_propagates_and_listeners_are_removed():
    page = Page(failure='cancelled')
    with pytest.raises(asyncio.CancelledError):
        confirm(page)
    assert not any(page.handlers.values())


def test_invalid_expected_account_fails_before_any_browser_read():
    page = Page()
    with pytest.raises(BusinessError):
        confirm(page, 'not a login')
    assert page.goto_calls == [] and page.cdp.calls == []


def test_signal_script_does_not_collect_credentials_or_whole_dom():
    for forbidden in ('document.cookie', 'localStorage', 'sessionStorage', 'innerHTML', 'outerHTML',
                      'textContent', 'input', 'password', 'screenshot', 'fetch(', '.value'):
        assert forbidden not in _READ_SIGNALS
    assert 'user-login' in _READ_SIGNALS and 'logged-in' in _READ_SIGNALS
