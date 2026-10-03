"""UTC persistence and monotonic elapsed time in an identifiable boot domain.

The boot identifier is read before opening a transaction. A fallback identifier
is intentionally process-local: a restart must conservatively reconcile UTC
time instead of treating an unrelated monotonic value as the same clock.
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
import hashlib
from pathlib import Path
import platform
import subprocess
import time
from typing import Protocol
import uuid

_PROCESS_DOMAIN = f'process:{uuid.uuid4().hex}'


class Clock(Protocol):
    @property
    def domain(self) -> str: ...

    def utcnow(self) -> datetime: ...

    def monotonic_ns(self) -> int: ...


@lru_cache(maxsize=1)
def _boot_domain() -> str:
    system = platform.system()
    try:
        if system == 'Linux':
            identity = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        elif system == 'Darwin':
            identity = subprocess.run(
                ['/usr/sbin/sysctl', '-n', 'kern.boottime'], check=True,
                capture_output=True, text=True, timeout=2,
            ).stdout.strip()
        else:
            identity = ''
        if identity:
            # Do not expose the host's raw boot identifier in persisted records.
            digest = hashlib.sha256(f'{system}:{identity}'.encode('utf-8')).hexdigest()
            return f'boot:{digest}'
    except (OSError, subprocess.SubprocessError, UnicodeError):
        pass
    return _PROCESS_DOMAIN


class SystemClock:
    def __init__(self):
        self._domain = _boot_domain()

    @property
    def domain(self) -> str:
        return self._domain

    def utcnow(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()
