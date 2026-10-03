"""Trusted process configuration, never accepted from webpage or task input."""
from dataclasses import dataclass
import json
import os

from .policy import DEFAULT_CONTROL_PORTS, Endpoint, NetworkDenied, NetworkPolicy


def _list(environ, name):
    raw = environ.get(name, '[]')
    try:
        if type(raw) is not str or len(raw) > 16384:
            raise ValueError
        value = json.loads(raw)
        if type(value) is not list or len(value) > 100:
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise NetworkDenied('invalid_policy') from None


@dataclass(frozen=True)
class NetworkConfig:
    webarena_endpoints: tuple[Endpoint, ...] = ()
    control_ports: frozenset[int] = DEFAULT_CONTROL_PORTS
    denied_endpoints: tuple[Endpoint, ...] = ()

    def __post_init__(self):
        policy = NetworkPolicy(webarena_endpoints=self.webarena_endpoints,
                               control_ports=self.control_ports, denied_endpoints=self.denied_endpoints)
        object.__setattr__(self, 'webarena_endpoints', policy.webarena_endpoints)
        object.__setattr__(self, 'control_ports', policy.control_ports)
        object.__setattr__(self, 'denied_endpoints', policy.denied_endpoints)

    @classmethod
    def from_env(cls, environ=None):
        environ = dict(os.environ if environ is None else environ)
        try:
            actual_ports = {int(environ.get('WEBAGENT_API_PORT', '8000')),
                            int(environ.get('WEBAGENT_UI_PORT', '5173')),
                            int(environ.get('WEBAGENT_PREVIEW_PORT', '4173'))}
            extra = _list(environ, 'WEBAGENT_NETWORK_ADMIN_PORTS')
            if any(type(port) is not int for port in extra):
                raise ValueError
            return cls(tuple(Endpoint.from_url(value) for value in _list(environ, 'WEBAGENT_WEBARENA_ORIGINS')),
                       DEFAULT_CONTROL_PORTS | actual_ports | frozenset(extra),
                       tuple(Endpoint.from_url(value) for value in _list(environ, 'WEBAGENT_NETWORK_DENIED_ORIGINS')))
        except (ValueError, TypeError):
            raise NetworkDenied('invalid_policy') from None

    def policy_for(self, realm):
        return NetworkPolicy(realm, self.webarena_endpoints, self.control_ports, self.denied_endpoints)
