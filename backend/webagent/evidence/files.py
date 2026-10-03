"""Private immutable blobs anchored with no-follow directory descriptors.

Publication uses the platform's atomic rename-with-no-replace primitive. Neither
the index nor these functions accept a filesystem path from an external caller.
The advisory lock spans publication through SQLite commit, so orphan scans never
race another cooperating process's not-yet-indexed artifact.
"""
from contextlib import contextmanager
import ctypes
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import time
from uuid import uuid4

from ..errors import BusinessError
from .models import MAX_ARTIFACT_BYTES, unavailable

_NAME = re.compile(r'[0-9a-f]{32}\.(?:blob|tmp)\Z', re.ASCII)
_FAULT_BYTES = b'0' + b'\0' * 4095


def _unsafe():
    return BusinessError('EVIDENCE_CORRUPT', 'Evidence filesystem is unsafe', status=409)


def _identity(fd):
    item = os.fstat(fd)
    return item.st_dev, item.st_ino


def _open_directory(path, *, create=False):
    """Walk every ancestor without following a symlink, including data_dir."""
    path = Path(os.path.abspath(os.fspath(path)))
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


class ArtifactFiles:
    def __init__(self, data_dir: Path, *, fault_hook=None, create=True):
        self.data_dir = Path(os.path.abspath(os.fspath(data_dir)))
        self.fault_hook = fault_hook
        self._faulted = False
        self._control_created = False
        self.data_fd = self.evidence_fd = self.blobs_fd = self.control_fd = None
        try:
            self.data_fd = _open_directory(self.data_dir, create=create)
            for name in ('evidence',):
                if create:
                    try:
                        os.mkdir(name, 0o700, dir_fd=self.data_fd)
                    except FileExistsError:
                        pass
            self.evidence_fd = os.open('evidence', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                       dir_fd=self.data_fd)
            for name in ('blobs', 'control'):
                if create:
                    try:
                        os.mkdir(name, 0o700, dir_fd=self.evidence_fd)
                        if name == 'control':
                            self._control_created = True
                    except FileExistsError:
                        pass
            self.blobs_fd = os.open('blobs', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=self.evidence_fd)
            self.control_fd = os.open('control', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                      dir_fd=self.evidence_fd)
            for fd in (self.evidence_fd, self.blobs_fd, self.control_fd):
                item = os.fstat(fd)
                if item.st_uid != os.geteuid() or item.st_mode & 0o077:
                    raise _unsafe()
            if self._control_created:
                with self.locked():
                    self._marker_init()
                os.fsync(self.control_fd)
                os.fsync(self.evidence_fd)
                os.fsync(self.data_fd)
            else:
                if create:
                    try:
                        os.stat('fault.marker', dir_fd=self.control_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        # Another process may have just made the control
                        # directory and not yet initialized its allocated
                        # marker. Wait only for that missing-file case; never
                        # recreate a missing control or repair malformed bytes.
                        deadline = time.monotonic() + 1
                        while True:
                            with self.locked():
                                try:
                                    os.stat('fault.marker', dir_fd=self.control_fd, follow_symlinks=False)
                                except FileNotFoundError:
                                    found = False
                                else:
                                    found = True
                            if found:
                                break
                            if time.monotonic() >= deadline:
                                raise unavailable()
                            time.sleep(.005)
                self.assert_available()
        except BaseException:
            self._faulted = True
            self.close()
            raise unavailable() from None

    def close(self):
        for name in ('control_fd', 'blobs_fd', 'evidence_fd', 'data_fd'):
            fd = getattr(self, name, None)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)

    def __del__(self):
        self.close()

    def _hook(self, stage):
        if self.fault_hook is not None:
            self.fault_hook(stage)

    def _verify_anchors(self):
        check = _open_directory(self.data_dir)
        children = []
        try:
            if _identity(check) != _identity(self.data_fd):
                raise _unsafe()
            parent = check
            for name, expected in (('evidence', self.evidence_fd), ('blobs', self.blobs_fd)):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                children.append(child)
                if _identity(child) != _identity(expected):
                    raise _unsafe()
                parent = child
            control = os.open('control', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=self.evidence_fd)
            children.append(control)
            if _identity(control) != _identity(self.control_fd):
                raise _unsafe()
        finally:
            for child in reversed(children):
                os.close(child)
            os.close(check)

    def _control(self, name, *, create=False):
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        if create:
            flags |= os.O_CREAT | os.O_EXCL
        fd = os.open(name, flags, 0o600, dir_fd=self.control_fd)
        item = os.fstat(fd)
        if (not stat.S_ISREG(item.st_mode) or item.st_nlink != 1 or item.st_uid != os.geteuid()
                or item.st_mode & 0o077):
            os.close(fd)
            raise _unsafe()
        return fd

    def _marker_init(self):
        if not self._control_created:
            return self.assert_available()
        try:
            fd = self._control('fault.marker.tmp', create=True)
        except FileExistsError:
            return self.assert_available()
        try:
            # Allocate a complete private block BEFORE the first artifact write.
            if hasattr(os, 'posix_fallocate'):
                os.posix_fallocate(fd, 0, len(_FAULT_BYTES))
            self._write_all(fd, _FAULT_BYTES)
            os.fsync(fd)
        finally:
            os.close(fd)
        self._rename_exclusive('fault.marker.tmp', 'fault.marker', directory_fd=self.control_fd)
        os.fsync(self.control_fd)

    def assert_available(self):
        if self._faulted:
            raise unavailable()
        try:
            self._verify_anchors()
            fd = self._control('fault.marker')
            try:
                if os.fstat(fd).st_size != len(_FAULT_BYTES) or os.pread(fd, len(_FAULT_BYTES), 0) != _FAULT_BYTES:
                    raise unavailable()
            finally:
                os.close(fd)
        except BaseException:
            self._faulted = True
            raise unavailable() from None

    def latch_fault(self):
        self._faulted = True
        if self.control_fd is None:
            return
        try:
            fd = self._control('fault.marker')
            try:
                if os.fstat(fd).st_size != len(_FAULT_BYTES):
                    return
                try:
                    if os.pwrite(fd, b'1', 0) != 1:
                        raise OSError(errno.EIO, 'Fault marker update failed')
                    os.fsync(fd)
                except OSError:
                    # Filesystems with copy-on-write may reject an in-place byte
                    # update even when its old block was allocated. A truncated
                    # control is invalid and therefore also blocks restart.
                    os.ftruncate(fd, 0)
                    os.fsync(fd)
            finally:
                os.close(fd)
        except (OSError, BusinessError):
            # Removing the marker cannot reinitialize the preexisting control
            # directory; the next process must fail closed on its absence.
            try:
                os.unlink('fault.marker', dir_fd=self.control_fd)
                os.fsync(self.control_fd)
            except OSError:
                pass  # Memory latch remains even if all durable writes fail.

    @contextmanager
    def locked(self):
        self._verify_anchors()
        try:
            fd = self._control('publication.lock', create=True)
            os.fsync(fd)
            os.fsync(self.control_fd)
        except FileExistsError:
            fd = self._control('publication.lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._verify_anchors()
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @staticmethod
    def _write_all(fd, data):
        view = memoryview(data)
        while view:
            amount = os.write(fd, view)
            if amount <= 0:
                raise OSError(errno.EIO, 'Artifact write failed')
            view = view[amount:]

    def _rename_exclusive(self, source, target, *, directory_fd=None):
        directory_fd = self.blobs_fd if directory_fd is None else directory_fd
        lib = ctypes.CDLL(None, use_errno=True)
        if sys.platform == 'darwin':
            fn, flags = lib.renameatx_np, 4  # RENAME_EXCL
        else:
            fn, flags = lib.renameat2, 1  # RENAME_NOREPLACE
        fn.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        fn.restype = ctypes.c_int
        if fn(directory_fd, source.encode(), directory_fd, target.encode(), flags):
            value = ctypes.get_errno()
            raise OSError(value, os.strerror(value))

    def publish(self, data: bytes):
        if type(data) is not bytes or len(data) > MAX_ARTIFACT_BYTES:
            raise BusinessError('INVALID_PARAMETER', 'Artifact bytes exceed the bound', field='data')
        self.assert_available()
        identifier = uuid4().hex
        temp, final = identifier + '.tmp', identifier + '.blob'
        fd = os.open(temp, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.blobs_fd)
        try:
            midpoint = len(data) // 2
            self._write_all(fd, data[:midpoint])
            self._hook('mid_write')
            self._write_all(fd, data[midpoint:])
            os.fsync(fd)
            actual = os.pread(fd, len(data) + 1, 0)
            if len(actual) != len(data) or hashlib.sha256(actual).digest() != hashlib.sha256(data).digest():
                raise _unsafe()
        finally:
            os.close(fd)
        self._verify_anchors()
        self._hook('before_rename')
        self._rename_exclusive(temp, final)
        os.fsync(self.blobs_fd)
        self._hook('after_rename')
        return 'evidence/blobs/' + final, hashlib.sha256(data).hexdigest(), len(data)

    @staticmethod
    def _name(artifact_path):
        if type(artifact_path) is not str or not artifact_path.startswith('evidence/blobs/'):
            raise _unsafe()
        name = artifact_path[len('evidence/blobs/'):]
        if not _NAME.fullmatch(name):
            raise _unsafe()
        return name

    def read(self, artifact_path, *, size_bytes, sha256):
        name = self._name(artifact_path)
        self._verify_anchors()
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.blobs_fd)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.geteuid()
                    or before.st_mode & 0o077 or before.st_size != size_bytes
                    or not 0 <= size_bytes <= MAX_ARTIFACT_BYTES):
                raise _unsafe()
            chunks, remaining = [], size_bytes + 1
            while remaining:
                chunk = os.read(fd, min(remaining, 1024 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b''.join(chunks)
            after = os.fstat(fd)
            path_stat = os.stat(name, dir_fd=self.blobs_fd, follow_symlinks=False)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise _unsafe()
            if (path_stat.st_dev, path_stat.st_ino) != (after.st_dev, after.st_ino):
                raise _unsafe()
            if len(data) != size_bytes or hashlib.sha256(data).hexdigest() != sha256:
                raise _unsafe()
            self._verify_anchors()
            return data
        finally:
            os.close(fd)

    def candidates(self):
        self._verify_anchors()
        result = []
        for name in os.listdir(self.blobs_fd):
            if not _NAME.fullmatch(name):
                continue
            item = os.stat(name, dir_fd=self.blobs_fd, follow_symlinks=False)
            if stat.S_ISREG(item.st_mode) and item.st_nlink == 1:
                result.append(('evidence/blobs/' + name, item.st_size, item.st_mtime,
                               (item.st_dev, item.st_ino, item.st_mtime_ns, item.st_size)))
        return result

    def unlink(self, artifact_path, expected_identity):
        self._verify_anchors()
        name = self._name(artifact_path)
        item = os.stat(name, dir_fd=self.blobs_fd, follow_symlinks=False)
        actual = item.st_dev, item.st_ino, item.st_mtime_ns, item.st_size
        if actual != expected_identity or not stat.S_ISREG(item.st_mode) or item.st_nlink != 1:
            raise _unsafe()
        os.unlink(name, dir_fd=self.blobs_fd)
        os.fsync(self.blobs_fd)
