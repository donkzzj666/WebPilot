"""Trusted site identity adapters. No API-selected URLs, selectors or model input.

GitHub's first-party head user-login signal is read only after a fresh GET of
/settings/profile. The public login response and first-party behavior bundle
were inspected without cookies; this is not a live-account acceptance claim.
The allowlist deliberately excludes Enterprise/managed-user username formats.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import re
from types import MappingProxyType
from urllib.parse import urlsplit

from ..errors import BusinessError

CONFIRM_TIMEOUT_SECONDS = 8
GITHUB_ORIGIN = 'https://github.com'
GITHUB_LOGIN_URL = GITHUB_ORIGIN + '/login?return_to=%2Fsettings%2Fprofile'
GITHUB_VERIFICATION_URL = GITHUB_ORIGIN + '/settings/profile'
ACCOUNT_PATTERN = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}\Z')
SITE_PATTERN = re.compile(r'[a-z][a-z0-9-]{0,63}\Z')


class IdentityStatus(StrEnum):
    VERIFIED = 'VERIFIED'
    NOT_AUTHENTICATED = 'NOT_AUTHENTICATED'
    ACCOUNT_MISMATCH = 'ACCOUNT_MISMATCH'
    UNVERIFIABLE = 'UNVERIFIABLE'


class IdentityReason(StrEnum):
    VERIFIED = 'verified'
    NOT_AUTHENTICATED = 'not_authenticated'
    ACCOUNT_MISMATCH = 'account_mismatch'
    WRONG_LOCATION = 'wrong_location'
    REDIRECTED = 'redirected'
    NAVIGATION_CHANGED = 'navigation_changed'
    SIGNAL_MISSING = 'signal_missing'
    SIGNAL_CONFLICT = 'signal_conflict'
    INVALID_ACCOUNT_SIGNAL = 'invalid_account_signal'
    RESPONSE_REJECTED = 'response_rejected'
    PAGE_UNAVAILABLE = 'page_unavailable'
    TIMEOUT = 'timeout'


@dataclass(frozen=True)
class IdentityObservation:
    status: IdentityStatus
    account: str | None
    reason: IdentityReason
    site_id: str
    realm: str
    verification_url: str | None = None
    evidence_sha256: str | None = None
    document_token: str | None = None

    @property
    def normalized_account(self):
        return self.account


# This script is constant and runs in a new isolated world. Never inspect input
# values, cookies/storage, textContent, whole DOM, response bodies or screenshots.
# Bound every page-controlled string before it crosses the browser protocol.
_READ_SIGNALS = r'''(() => {
  const direct = (name) => {
    const nodes = document.head ? document.head.querySelectorAll(':scope > meta[name="' + name + '"]') : [];
    return {count:nodes.length, values:Array.from(nodes).slice(0,2).map(node => {
      const value=node.getAttribute('content');
      return typeof value === 'string' && value.length <= 255 ? value : null;
    })};
  };
  const body=document.body;
  return {
    url:location.href.length <= 2048 ? location.href : null,
    ready:document.readyState,
    login:direct('user-login'), hostname:direct('hostname'), expectedHost:direct('expected-hostname'),
    loggedIn:!!body && body.classList.contains('logged-in'),
    loggedOut:!!body && body.classList.contains('logged-out'),
    loginForm:!!document.querySelector('form[action="/session"], form[action="/login"], form[action^="/sessions/"]')
  };
})()'''


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True).encode()).hexdigest()


@dataclass(frozen=True)
class SiteAdapter:
    site_id: str = 'github'
    realm: str = 'public'
    login_url: str = GITHUB_LOGIN_URL
    verification_url: str = GITHUB_VERIFICATION_URL

    def __post_init__(self):
        if type(self) is SiteAdapter and (self.site_id, self.realm, self.login_url, self.verification_url) != (
                'github', 'public', GITHUB_LOGIN_URL, GITHUB_VERIFICATION_URL):
            raise ValueError('Unsupported production identity site configuration')

    @property
    def origin(self):
        parsed = urlsplit(self.verification_url)
        return parsed.scheme + '://' + parsed.netloc

    @property
    def adapter_id(self):
        return 'fixture-github-auth-v1' if isinstance(self, FixtureSiteAdapter) else 'github-auth-v1'

    def normalize_account(self, value: str) -> str:
        if type(value) is not str or not ACCOUNT_PATTERN.fullmatch(value):
            raise BusinessError('INVALID_PARAMETER', 'Expected account must be a supported site username',
                                field='expected_account')
        return value.lower()

    def public_metadata(self):
        return {'site_id': self.site_id, 'realm': self.realm, 'login_url': self.login_url,
                'verification_url': self.verification_url, 'adapter_id': self.adapter_id}

    def _observation(self, status, reason, *, account=None, verified_location=False,
                     evidence=None, document=None):
        return IdentityObservation(status, account, reason, self.site_id, self.realm,
            self.verification_url if verified_location else None, evidence, document)

    def _reject(self, reason):
        return self._observation(IdentityStatus.UNVERIFIABLE, reason)

    def _login_location(self, value):
        try:
            target, expected = urlsplit(value), urlsplit(self.login_url)
            return (target.scheme == expected.scheme and target.netloc == expected.netloc
                    and target.path == expected.path and not target.fragment
                    and target.username is None and target.password is None)
        except (ValueError, TypeError):
            return False

    async def confirm(self, page, expected_account: str) -> IdentityObservation:
        """Explicit, bounded confirmation window. Never run while the user types.

        Every call refreshes the one trusted verification page. Callers may
        compare stable evidence across separate confirmations; document_token
        intentionally changes after a full navigation.
        """
        expected = self.normalize_account(expected_account)
        try:
            async with asyncio.timeout(CONFIRM_TIMEOUT_SECONDS):
                return await self._confirm(page, expected)
        except TimeoutError:
            return self._reject(IdentityReason.TIMEOUT)
        except Exception:
            # Browser exception text may contain URLs or page values. Do not
            # retain, log, chain, or return it. Cancellation still propagates.
            return self._reject(IdentityReason.PAGE_UNAVAILABLE)

    async def _confirm(self, page, expected):
        if page.is_closed():
            return self._reject(IdentityReason.PAGE_UNAVAILABLE)
        requests = []
        navigations = []
        invalid = False
        closed = False

        def requested(request):
            nonlocal invalid
            try:
                if request.is_navigation_request() and request.frame == page.main_frame:
                    if len(requests) < 2:
                        requests.append(request)
                    if len(requests) > 1 or request.url != self.verification_url or request.method != 'GET':
                        invalid = True
            except Exception:
                invalid = True

        def navigated(frame):
            nonlocal invalid
            if frame == page.main_frame:
                if len(navigations) < 2:
                    navigations.append(frame)
                if len(navigations) > 1 or frame.url != self.verification_url:
                    invalid = True

        def unavailable(*_):
            nonlocal closed
            closed = True

        listeners = [('request', requested), ('framenavigated', navigated),
                     ('close', unavailable), ('crash', unavailable)]
        for event, callback in listeners:
            page.on(event, callback)
        cdp = None
        try:
            response = await page.goto(self.verification_url, wait_until='load', timeout=5000)
            final_url = page.url
            if self._login_location(final_url):
                return self._observation(IdentityStatus.NOT_AUTHENTICATED, IdentityReason.NOT_AUTHENTICATED)
            if final_url != self.verification_url:
                return self._reject(IdentityReason.WRONG_LOCATION)
            if response is None or response.url != self.verification_url:
                return self._reject(IdentityReason.RESPONSE_REJECTED)
            if response.request.redirected_from is not None:
                return self._reject(IdentityReason.REDIRECTED)
            if response.status in (401, 403):
                return self._observation(IdentityStatus.NOT_AUTHENTICATED, IdentityReason.NOT_AUTHENTICATED)
            content_type = await response.header_value('content-type')
            if (response.status != 200 or response.from_service_worker
                    or not isinstance(content_type, str) or content_type.split(';', 1)[0].strip().lower() != 'text/html'):
                return self._reject(IdentityReason.RESPONSE_REJECTED)
            if invalid or closed or len(requests) != 1 or len(navigations) != 1:
                return self._reject(IdentityReason.NAVIGATION_CHANGED)
            cdp = await page.context.new_cdp_session(page)
            frame = (await cdp.send('Page.getFrameTree'))['frameTree']['frame']
            if frame.get('url') != self.verification_url or not frame.get('loaderId'):
                return self._reject(IdentityReason.NAVIGATION_CHANGED)
            world = await cdp.send('Page.createIsolatedWorld', {'frameId': frame['id'],
                'worldName': 'webpilot-identity-confirmation'})
            params = {'expression': _READ_SIGNALS, 'contextId': world['executionContextId'],
                      'returnByValue': True, 'awaitPromise': False}
            first = await cdp.send('Runtime.evaluate', params)
            # Let pending navigation events arrive; verify both signals and
            # document loader again instead of trusting a transient account.
            await asyncio.sleep(.03)
            second = await cdp.send('Runtime.evaluate', params)
            after = (await cdp.send('Page.getFrameTree'))['frameTree']['frame']
            if (invalid or closed or page.is_closed() or page.url != self.verification_url
                    or len(requests) != 1 or len(navigations) != 1
                    or frame.get('id') != after.get('id') or frame.get('loaderId') != after.get('loaderId')
                    or after.get('url') != self.verification_url):
                return self._reject(IdentityReason.NAVIGATION_CHANGED)
            if first.get('exceptionDetails') or second.get('exceptionDetails'):
                return self._reject(IdentityReason.PAGE_UNAVAILABLE)
            one, two = first.get('result', {}).get('value'), second.get('result', {}).get('value')
            if one != two:
                return self._reject(IdentityReason.SIGNAL_CONFLICT)
            return self._interpret(one, expected, _digest({'loader': frame['loaderId'], 'frame': frame['id']}))
        finally:
            for event, callback in listeners:
                page.remove_listener(event, callback)
            if cdp is not None:
                # Local protocol cleanup is bounded independently of a page
                # failure. No error payload from the browser is surfaced.
                try:
                    async with asyncio.timeout(.5):
                        await cdp.detach()
                except Exception:
                    pass

    def _interpret(self, data, expected, document):
        if not isinstance(data, dict) or set(data) != {'url', 'ready', 'login', 'hostname', 'expectedHost',
                                                       'loggedIn', 'loggedOut', 'loginForm'}:
            return self._reject(IdentityReason.SIGNAL_MISSING)
        if data['url'] != self.verification_url or data['ready'] != 'complete':
            return self._reject(IdentityReason.NAVIGATION_CHANGED)
        if any(type(data[key]) is not bool for key in ('loggedIn', 'loggedOut', 'loginForm')):
            return self._reject(IdentityReason.SIGNAL_MISSING)
        for key in ('login', 'hostname', 'expectedHost'):
            signal = data[key]
            if (not isinstance(signal, dict) or set(signal) != {'count', 'values'}
                    or type(signal['count']) is not int or not 0 <= signal['count'] <= 1
                    or not isinstance(signal['values'], list) or len(signal['values']) != signal['count']):
                return self._reject(IdentityReason.SIGNAL_CONFLICT)
        if data['loggedIn'] and (data['loggedOut'] or data['loginForm']):
            return self._reject(IdentityReason.SIGNAL_CONFLICT)
        if not data['loggedIn'] or data['loggedOut'] or data['loginForm']:
            return self._observation(IdentityStatus.NOT_AUTHENTICATED, IdentityReason.NOT_AUTHENTICATED)
        if data['login']['count'] != 1:
            return self._reject(IdentityReason.SIGNAL_MISSING)
        account = data['login']['values'][0]
        try:
            account = self.normalize_account(account)
        except BusinessError:
            return self._reject(IdentityReason.INVALID_ACCOUNT_SIGNAL)
        host = urlsplit(self.verification_url).hostname
        for key in ('hostname', 'expectedHost'):
            if data[key]['count'] and data[key]['values'] != [host]:
                return self._reject(IdentityReason.SIGNAL_CONFLICT)
        evidence = _digest({'schema': 'identity-signals-v1', 'site_id': self.site_id,
                            'realm': self.realm, 'url': self.verification_url,
                            'account': account, 'authenticated': True})
        if account != expected:
            return self._observation(IdentityStatus.ACCOUNT_MISMATCH, IdentityReason.ACCOUNT_MISMATCH,
                                     account=account, verified_location=True, evidence=evidence, document=document)
        return self._observation(IdentityStatus.VERIFIED, IdentityReason.VERIFIED,
                                 account=account, verified_location=True, evidence=evidence, document=document)


GitHubSiteAdapter = SiteAdapter


class FixtureSiteAdapter(SiteAdapter):
    """Explicit Python-only synthetic catalog entry with the same fixed signals."""
    def __init__(self, *, site_id: str, origin: str, login_path='/login', verification_path='/identity'):
        try:
            parsed = urlsplit(origin)
            valid = (SITE_PATTERN.fullmatch(site_id) and parsed.scheme == 'http'
                     and parsed.hostname in ('127.0.0.1', '::1') and parsed.port is not None
                     and 1024 <= parsed.port <= 65535 and origin == 'http://' + parsed.netloc
                     and parsed.username is None and parsed.password is None)
            for path in (login_path, verification_path):
                valid = valid and bool(re.fullmatch(r'/[a-zA-Z0-9/_-]+', path)) and '//' not in path
            if not valid or login_path == verification_path:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValueError('Invalid synthetic identity site configuration') from None
        super().__init__(site_id, 'webarena', origin + login_path, origin + verification_path)


class SiteCatalog:
    def __init__(self, adapters=None):
        entries = (SiteAdapter(),) if adapters is None else tuple(adapters)
        if not entries or any(not isinstance(entry, SiteAdapter) for entry in entries):
            raise ValueError('Invalid identity site catalog')
        mapping = {entry.site_id: entry for entry in entries}
        if len(mapping) != len(entries):
            raise ValueError('Duplicate identity site catalog entry')
        self._entries = MappingProxyType(mapping)

    def get(self, site_id: str) -> SiteAdapter:
        if type(site_id) is not str or site_id not in self._entries:
            raise BusinessError('INVALID_PARAMETER', 'Identity site is not supported', field='site_id')
        return self._entries[site_id]

    def list(self):
        return [entry.public_metadata() for entry in self._entries.values()]
