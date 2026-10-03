"""Resolve once, validate every answer, connect a selected IP, then check peer.

Public addresses are deliberately conservative across Python/IANA registry
versions. WebArena permits only administrator-registered origin triples; its
private-address exception never applies to control or management ports.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import ipaddress
import re
import socket
from typing import Awaitable, Callable, Sequence
from urllib.parse import urlsplit

DEFAULT_CONTROL_PORTS = frozenset({8000, 5173, 4173, 9222, 9333})
Resolver = Callable[[str, int], Awaitable[Sequence[str]]]
_REASONS = frozenset({'invalid_target', 'unsupported_scheme', 'control_plane', 'not_registered',
    'dns_failed', 'unsafe_address', 'peer_mismatch', 'invalid_policy', 'proxy_loop'})


class NetworkDenied(Exception):
    def __init__(self, reason='invalid_target'):
        self.reason = reason if reason in _REASONS else 'invalid_target'
        super().__init__('Network request denied: ' + self.reason)


def normalize_host(value):
    if type(value) is not str or not value or len(value) > 253 or any(
            ord(c) <= 32 or ord(c) == 127 or c in '/\\@%?#' for c in value):
        raise NetworkDenied('invalid_target')
    if value.startswith('[') and value.endswith(']'):
        value = value[1:-1]
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        if ':' in value:
            raise NetworkDenied('invalid_target') from None
    try:
        value = value.removesuffix('.').encode('idna').decode('ascii').lower()
    except (UnicodeError, ValueError):
        raise NetworkDenied('invalid_target') from None
    if not value or len(value) > 253 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                                           for label in value.split('.')):
        raise NetworkDenied('invalid_target')
    return value


@dataclass(frozen=True)
class Endpoint:
    scheme: str
    host: str
    port: int

    def __post_init__(self):
        if self.scheme not in ('http', 'https'):
            raise NetworkDenied('unsupported_scheme')
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise NetworkDenied('invalid_target')
        object.__setattr__(self, 'host', normalize_host(self.host))

    @property
    def authority(self):
        host = '[' + self.host + ']' if ':' in self.host else self.host
        return host + ':' + str(self.port)

    @classmethod
    def from_url(cls, value):
        endpoint, parsed = parse_url(value)
        if parsed.path not in ('', '/') or parsed.query or parsed.fragment:
            raise NetworkDenied('invalid_policy')
        return endpoint


def parse_url(value):
    if type(value) is not str or not value or len(value) > 16384 or any(
            ord(c) <= 32 or ord(c) == 127 or c == '\\' for c in value):
        raise NetworkDenied('invalid_target')
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.netloc or not parsed.hostname
                or parsed.username is not None or parsed.password is not None or parsed.fragment
                or '%' in parsed.netloc or parsed.netloc.endswith(':')):
            raise NetworkDenied('invalid_target')
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == 'https' else 80)
        return Endpoint(parsed.scheme, parsed.hostname, port), parsed
    except (ValueError, UnicodeError):
        raise NetworkDenied('invalid_target') from None


@dataclass(frozen=True)
class ResolvedTarget:
    endpoint: Endpoint
    addresses: tuple[str, ...]

    @property
    def scheme(self): return self.endpoint.scheme

    @property
    def host(self): return self.endpoint.host

    @property
    def port(self): return self.endpoint.port


async def system_resolver(host: str, port: int) -> tuple[str, ...]:
    result = await asyncio.get_running_loop().getaddrinfo(host, port, family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    return tuple(dict.fromkeys(item[4][0] for item in result))


def _address(value):
    if type(value) is not str or '%' in value:
        raise NetworkDenied('unsafe_address')
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise NetworkDenied('unsafe_address') from None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        raise NetworkDenied('unsafe_address')
    return address


_PUBLIC_EXCLUSIONS = tuple(ipaddress.ip_network(value) for value in (
    '192.0.0.0/24', '192.88.99.0/24', '2001::/23', '2002::/16', '3fff::/20',
))


def public_address(value):
    address = _address(value)
    if (not address.is_global or address.is_multicast or address.is_reserved or address.is_loopback
            or address.is_link_local or address.is_unspecified or getattr(address, 'is_site_local', False)
            or any(address.version == network.version and address in network for network in _PUBLIC_EXCLUSIONS)
            or (address.version == 6 and address not in ipaddress.ip_network('2000::/3'))):
        raise NetworkDenied('unsafe_address')
    return str(address)


@dataclass(frozen=True)
class NetworkPolicy:
    realm: str = 'public'
    webarena_endpoints: tuple[Endpoint, ...] = ()
    control_ports: frozenset[int] = DEFAULT_CONTROL_PORTS
    denied_endpoints: tuple[Endpoint, ...] = ()
    resolver: Resolver | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self.realm not in ('public', 'webarena'):
            raise NetworkDenied('invalid_policy')
        try:
            allowed = tuple(Endpoint(e.scheme, e.host, e.port) for e in self.webarena_endpoints)
            denied = tuple(Endpoint(e.scheme, e.host, e.port) for e in self.denied_endpoints)
            ports = frozenset(self.control_ports) | DEFAULT_CONTROL_PORTS | {e.port for e in denied}
            if any(type(port) is not int or not 1 <= port <= 65535 for port in ports):
                raise ValueError
        except (AttributeError, TypeError, ValueError):
            raise NetworkDenied('invalid_policy') from None
        object.__setattr__(self, 'webarena_endpoints', allowed)
        object.__setattr__(self, 'denied_endpoints', denied)
        object.__setattr__(self, 'control_ports', ports)

    def check_endpoint(self, endpoint: Endpoint):
        endpoint = Endpoint(endpoint.scheme, endpoint.host, endpoint.port)
        if endpoint.port in self.control_ports:
            raise NetworkDenied('control_plane')
        if self.realm == 'public' and (endpoint.host == 'localhost' or endpoint.host.endswith('.localhost')):
            raise NetworkDenied('unsafe_address')
        if self.realm == 'webarena' and endpoint not in self.webarena_endpoints:
            raise NetworkDenied('not_registered')
        # HTTPS CONNECT is restricted to normal TLS web traffic on public sites.
        if self.realm == 'public' and endpoint.scheme == 'https' and endpoint.port != 443:
            raise NetworkDenied('not_registered')
        return endpoint

    def check_address(self, address):
        if self.realm == 'public':
            return public_address(address)
        value = _address(address)
        if (value.is_multicast or value.is_link_local or value.is_unspecified or (value.is_reserved and not value.is_loopback)
                or getattr(value, 'is_site_local', False)):
            raise NetworkDenied('unsafe_address')
        return str(value)

    async def resolve(self, url: str) -> ResolvedTarget:
        endpoint = self.check_endpoint(parse_url(url)[0])
        try:
            numeric = ipaddress.ip_address(endpoint.host)
        except ValueError:
            try:
                async with asyncio.timeout(10):
                    answers = await (self.resolver or system_resolver)(endpoint.host, endpoint.port)
            except Exception:
                raise NetworkDenied('dns_failed') from None
        else:
            answers = (str(numeric),)
        if not isinstance(answers, (list, tuple)) or not 1 <= len(answers) <= 64:
            raise NetworkDenied('dns_failed')
        # Reject the whole answer set, never fall back from a private answer to
        # a public one: DNS rotation cannot bypass a denied destination.
        addresses = tuple(dict.fromkeys(self.check_address(value) for value in answers))
        return ResolvedTarget(endpoint, addresses)

    def validate_peer(self, target: ResolvedTarget, selected_ip: str, peer):
        self.check_endpoint(target.endpoint)
        selected = self.check_address(selected_ip)
        if selected not in target.addresses or not isinstance(peer, tuple) or len(peer) < 2:
            raise NetworkDenied('peer_mismatch')
        actual = self.check_address(peer[0])
        if actual != selected or type(peer[1]) is not int or peer[1] != target.port:
            raise NetworkDenied('peer_mismatch')
