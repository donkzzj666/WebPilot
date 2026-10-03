"""Destination checks are exercised without DNS/network access."""
import asyncio
from dataclasses import replace
import json

import pytest

from webagent.network.config import NetworkConfig
from webagent.network.policy import Endpoint, NetworkDenied, NetworkPolicy, parse_url


@pytest.mark.parametrize('address', [
    '0.0.0.0', '0.1.2.3', '10.0.0.1', '127.0.0.1', '127.50.2.3', '169.254.169.254',
    '172.16.0.1', '172.31.255.254', '192.168.0.1', '192.0.0.9', '192.0.2.1',
    '192.88.99.1', '198.18.0.1', '198.51.100.1', '203.0.113.1', '100.64.0.1',
    '224.0.0.1', '239.1.2.3', '240.0.0.1', '255.255.255.255', '::', '::1',
    '::ffff:127.0.0.1', '::ffff:8.8.8.8', 'fc00::1', 'fd00::1', 'fe80::1',
    'fec0::1', 'ff02::1', '2001:db8::1', '2001::1', '2002:0808:0808::1',
    '64:ff9b::808:808', '64:ff9b:1::1', '3fff::1',
])
def test_public_policy_rejects_all_special_destination_classes(address):
    async def resolve(*_): return [address]
    with pytest.raises(NetworkDenied) as denied:
        asyncio.run(NetworkPolicy(resolver=resolve).resolve('http://example.test/resource'))
    assert denied.value.reason == 'unsafe_address'


@pytest.mark.parametrize('address', ['8.8.8.8', '1.1.1.1', '2606:4700:4700::1111'])
def test_public_policy_accepts_global_unicast_and_peer_must_match(address):
    async def resolve(*_): return [address]
    policy = NetworkPolicy(resolver=resolve)
    target = asyncio.run(policy.resolve('https://example.test/resource'))
    assert target.addresses == (address,)
    policy.validate_peer(target, address, (address, 443))
    for peer in [('1.0.0.1', 443), (address, 444), None, ('127.0.0.1', 443)]:
        with pytest.raises(NetworkDenied):
            policy.validate_peer(target, address, peer)


def test_mixed_dns_answers_fail_as_a_whole_without_selecting_the_public_answer():
    async def resolver(*_): return ['8.8.8.8', '127.0.0.1']
    with pytest.raises(NetworkDenied) as denied:
        asyncio.run(NetworkPolicy(resolver=resolver).resolve('https://mixed.test/'))
    assert denied.value.reason == 'unsafe_address'


def test_one_resolution_is_frozen_and_cannot_rebind_during_peer_check():
    calls = []
    async def resolver(host, port):
        calls.append((host, port))
        return ['8.8.8.8'] if len(calls) == 1 else ['127.0.0.1']
    policy = NetworkPolicy(resolver=resolver)
    target = asyncio.run(policy.resolve('https://rebinding.test/'))
    policy.validate_peer(target, '8.8.8.8', ('8.8.8.8', 443))
    assert calls == [('rebinding.test', 443)]
    with pytest.raises(NetworkDenied):
        policy.validate_peer(target, '127.0.0.1', ('127.0.0.1', 443))


@pytest.mark.parametrize('url', [
    'file:///etc/passwd', 'data:text/html,hello', 'ws://example.test/', 'ftp://example.test/',
    'http://user:pass@example.test/', 'http://example.test:0/', 'http://example.test:/',
    'http://example.test:65536/', 'http://example.test\\@127.0.0.1/',
    'http://127%2e0%2e0%2e1/', 'http://[fe80::1%lo0]/', 'http://example.test/\r\nheader',
    'http://example.test/#fragment', '//example.test/', 'http://example.test/ space',
    'http://[::ffff:127.0.0.1]/', 'http://2130706433/', 'http://0177.0.0.1/',
])
def test_ambiguous_urls_and_address_aliases_never_reach_public_destination(url):
    async def loopback(*_): return ['127.0.0.1']
    with pytest.raises(NetworkDenied):
        asyncio.run(NetworkPolicy(resolver=loopback).resolve(url))


def test_webarena_is_exact_scheme_host_port_and_default_deny():
    endpoint = Endpoint('http', '127.0.0.1', 19001)
    policy = NetworkPolicy('webarena', (endpoint,))
    assert asyncio.run(policy.resolve('http://127.0.0.1:19001/allowed?q=1')).endpoint == endpoint
    for url in ('http://127.0.0.1:19002/', 'https://127.0.0.1:19001/', 'http://localhost:19001/'):
        with pytest.raises(NetworkDenied) as denied:
            asyncio.run(policy.resolve(url))
        assert denied.value.reason == 'not_registered'
    with pytest.raises(NetworkDenied):
        asyncio.run(NetworkPolicy('webarena').resolve('http://127.0.0.1:19001/'))


@pytest.mark.parametrize('address', ['::ffff:127.0.0.1', '::ffff:8.8.8.8', '169.254.169.254', '224.0.0.1', '0.0.0.0'])
def test_webarena_registration_never_enables_mapped_metadata_or_multicast(address):
    async def resolver(*_): return [address]
    endpoint = Endpoint('http', 'business.test', 19001)
    with pytest.raises(NetworkDenied):
        asyncio.run(NetworkPolicy('webarena', (endpoint,), resolver=resolver).resolve('http://business.test:19001/'))


def test_local_control_and_management_ports_override_misregistration_and_dns_aliases():
    endpoint = Endpoint('http', 'business-alias.test', 19002)
    admin = Endpoint('https', 'management.test', 19002)
    calls = []
    async def resolver(*_):
        calls.append(True)
        return ['127.0.0.1']
    policy = NetworkPolicy('webarena', (endpoint,), denied_endpoints=(admin,), resolver=resolver)
    with pytest.raises(NetworkDenied) as denied:
        asyncio.run(policy.resolve('http://business-alias.test:19002/'))
    assert denied.value.reason == 'control_plane' and calls == []
    for port in (8000, 5173, 4173, 9222, 9333):
        registered = Endpoint('http', '127.0.0.1', port)
        with pytest.raises(NetworkDenied):
            asyncio.run(NetworkPolicy('webarena', (registered,)).resolve('http://' + registered.authority))


def test_process_network_config_defaults_deny_webarena_and_includes_actual_control_ports():
    config = NetworkConfig.from_env({'WEBAGENT_API_PORT': '19201', 'WEBAGENT_UI_PORT': '19202',
                                    'WEBAGENT_PREVIEW_PORT': '19203'})
    assert {19201, 19202, 19203, 8000, 5173, 4173, 9222} <= config.control_ports
    assert config.policy_for('webarena').webarena_endpoints == ()
    configured = NetworkConfig.from_env({'WEBAGENT_WEBARENA_ORIGINS': '["http://127.0.0.1:19001"]',
        'WEBAGENT_NETWORK_DENIED_ORIGINS': '["https://management.test:19002"]',
        'WEBAGENT_NETWORK_ADMIN_PORTS': '[19003]'})
    assert configured.webarena_endpoints == (Endpoint('http', '127.0.0.1', 19001),)
    assert {19002, 19003} <= configured.control_ports


@pytest.mark.parametrize('value', ['null', '{}', '["file:///private"]', '["http://x.test/private"]',
                                  '["http://user:pass@x.test/"]', '["http://x.test:0"]'])
def test_bad_trusted_origin_configuration_is_rejected_without_echoing_value(value):
    with pytest.raises(NetworkDenied) as denied:
        NetworkConfig.from_env({'WEBAGENT_WEBARENA_ORIGINS': value})
    assert value not in str(denied.value)


def test_policy_copies_allowlists_and_does_not_include_resolver_secrets_in_repr():
    endpoints = [Endpoint('http', '127.0.0.1', 19001)]
    policy = NetworkPolicy('webarena', endpoints)
    endpoints.append(Endpoint('http', '127.0.0.1', 19002))
    assert len(policy.webarena_endpoints) == 1
