"""Persistent local bearer secret. Never exported to environment, URLs or logs."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import secrets
import stat

TOKEN_DIRECTORY = '.security'
TOKEN_FILE = 'local-api-token'
TOKEN_PATTERN = re.compile(r'[A-Za-z0-9_-]{64}\Z')


class LocalTokenError(RuntimeError):
    def __init__(self):
        super().__init__('Local API credential storage is unavailable or unsafe')


def _private_file(fd: int) -> None:
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise LocalTokenError()


@contextmanager
def _directory(path: Path):
    if not path.is_absolute() or '..' in path.parts:
        raise LocalTokenError()
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                os.mkdir(part, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise LocalTokenError()
        try:
            os.mkdir(TOKEN_DIRECTORY, 0o700, dir_fd=descriptor)
        except FileExistsError:
            pass
        child = os.open(TOKEN_DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
        os.close(descriptor)
        descriptor = child
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise LocalTokenError()
        yield descriptor
    finally:
        os.close(descriptor)


def load_or_create_token(data_dir: Path) -> str:
    """Create once under a locked, private directory; reject corrupt/unsafe files."""
    try:
        with _directory(data_dir) as directory:
            # Exclusive creation avoids the macOS O_CREAT|O_NOFOLLOW race where
            # concurrent first opens can spuriously fail with ENOENT. A loser
            # opens the existing inode without creation and validates it below.
            try:
                lock = os.open('local-api.lock', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                               0o600, dir_fd=directory)
            except FileExistsError:
                lock = os.open('local-api.lock', os.O_RDWR | os.O_NOFOLLOW, dir_fd=directory)
            try:
                _private_file(lock)
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    descriptor = os.open(TOKEN_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                except FileNotFoundError:
                    descriptor = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                         0o600, dir_fd=directory)
                    try:
                        _private_file(descriptor)
                        raw = secrets.token_urlsafe(48).encode('ascii')
                        if os.write(descriptor, raw) != len(raw):
                            raise LocalTokenError()
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                    os.fsync(directory)
                    descriptor = os.open(TOKEN_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                try:
                    _private_file(descriptor)
                    raw = os.read(descriptor, 65)
                    token = raw.decode('ascii')
                    if not TOKEN_PATTERN.fullmatch(token):
                        raise LocalTokenError()
                    return token
                finally:
                    os.close(descriptor)
            finally:
                os.close(lock)
    except (OSError, ValueError, UnicodeError):
        raise LocalTokenError() from None


if __name__ == '__main__':
    from ..config import Settings
    load_or_create_token(Settings.from_env().data_dir)
