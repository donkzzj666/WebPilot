"""Frozen product ceilings and bounded budget metadata.

A contract may tighten these limits. It cannot raise a product ceiling or lower
an enforced site/CI interval. Recovery keys exclude free-form error descriptions.
"""
from enum import StrEnum
from ..tasks.models import BudgetProfile


class ObstacleType(StrEnum):
    LOCATOR_CHANGED = 'locator_changed'
    PAGE_NOT_READY = 'page_not_ready'
    NETWORK_ERROR = 'network_error'
    BUSINESS_VALIDATION = 'business_validation'
    LOGIN_EXPIRED = 'login_expired'
    BROWSER_CHALLENGE = 'browser_challenge'
    RATE_LIMIT = 'rate_limit'
    PERMISSION_DENIED = 'permission_denied'
    WRITE_UNKNOWN = 'write_unknown'
    WORKER_INTERRUPTED = 'worker_interrupted'


MONITOR_SOURCES = frozenset(('security_community', 'cisa_kev'))
CONSUMPTION_KINDS = frozenset(('action', 'observation', 'screenshot', 'recovery', 'ci_poll'))
LIMIT_FIELDS = tuple(BudgetProfile.model_fields)
