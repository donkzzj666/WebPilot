"""Private filesystem boundaries: no caller paths, unsafe links or parent swaps."""
from pathlib import Path
import os
from uuid import uuid4

import pytest

from webagent.errors import BusinessError
from webagent.evidence.files import ArtifactFiles


def artifact(files, data=b'synthetic'):
    with files.locked():
        return files.publish(data)


def test_atomic_no_replace_never_overwrites_existing_blob(tmp_path):
    files = ArtifactFiles(tmp_path)
    source, target = 'a' * 32 + '.tmp', 'b' * 32 + '.blob'
    (tmp_path / 'evidence/blobs' / source).write_bytes(b'new')
    (tmp_path / 'evidence/blobs' / target).write_bytes(b'old')
    with pytest.raises(FileExistsError):
        files._rename_exclusive(source, target)
    assert (tmp_path / 'evidence/blobs' / source).read_bytes() == b'new'
    assert (tmp_path / 'evidence/blobs' / target).read_bytes() == b'old'


@pytest.mark.parametrize('path', ['/etc/passwd', '../x', 'evidence/blobs/../../x',
    'evidence/blobs/x.blob', 'evidence/blobs/' + 'a' * 32 + '.blob/child',
    'evidence/blobs/' + 'a' * 32 + '.blob\0', 'evidence//blobs/' + 'a' * 32 + '.blob'])
def test_local_paths_cannot_be_read(tmp_path, path):
    files = ArtifactFiles(tmp_path)
    with pytest.raises(BusinessError) as caught:
        files.read(path, size_bytes=1, sha256='a' * 64)
    assert caught.value.code == 'EVIDENCE_CORRUPT'


@pytest.mark.parametrize('component', ['data', 'evidence', 'blobs', 'control'])
def test_symlink_directory_boundaries_fail_closed(tmp_path, component):
    external = tmp_path / 'external'
    external.mkdir()
    data = tmp_path / 'data'
    if component == 'data':
        data.symlink_to(external)
    else:
        data.mkdir()
        if component == 'evidence':
            (data / 'evidence').symlink_to(external)
        else:
            (data / 'evidence').mkdir(mode=0o700)
            (data / 'evidence' / component).symlink_to(external)
    with pytest.raises(BusinessError) as caught:
        ArtifactFiles(data)
    assert caught.value.code == 'EVIDENCE_STORAGE_UNAVAILABLE'


@pytest.mark.parametrize('component', ['evidence', 'blobs', 'data'])
def test_parent_replaced_after_open_never_returns_bytes(tmp_path, component):
    data = tmp_path / 'data'
    files = ArtifactFiles(data)
    path, digest, size = artifact(files)
    target = data if component == 'data' else data / 'evidence' if component == 'evidence' else data / 'evidence/blobs'
    target.rename(target.with_name(target.name + '-old'))
    target.mkdir(mode=0o700)
    with pytest.raises((BusinessError, OSError)):
        files.read(path, size_bytes=size, sha256=digest)


def test_parent_swap_during_read_rejected_before_bytes_leave(tmp_path, monkeypatch):
    files = ArtifactFiles(tmp_path)
    path, digest, size = artifact(files)
    original_read = os.read
    fired = False
    def swap(fd, amount):
        nonlocal fired
        result = original_read(fd, amount)
        if not fired:
            fired = True
            (tmp_path / 'evidence/blobs').rename(tmp_path / 'evidence/old-blobs')
            (tmp_path / 'evidence/blobs').mkdir(mode=0o700)
        return result
    monkeypatch.setattr(os, 'read', swap)
    with pytest.raises(BusinessError):
        files.read(path, size_bytes=size, sha256=digest)


def test_file_replaced_midread_rejected(tmp_path, monkeypatch):
    files = ArtifactFiles(tmp_path)
    path, digest, size = artifact(files)
    original_read = os.read
    fired = False
    def swap(fd, amount):
        nonlocal fired
        result = original_read(fd, amount)
        if not fired:
            fired = True
            replacement = tmp_path / 'replacement'
            replacement.write_bytes(b'synthetic')
            replacement.replace(tmp_path / path)
        return result
    monkeypatch.setattr(os, 'read', swap)
    with pytest.raises(BusinessError):
        files.read(path, size_bytes=size, sha256=digest)


def test_private_modes_and_no_input_bytes_in_fault_control(tmp_path):
    files = ArtifactFiles(tmp_path)
    path, _, _ = artifact(files, b'fake-secret-123')
    assert (tmp_path / path).stat().st_mode & 0o777 == 0o600
    assert (tmp_path / 'evidence').stat().st_mode & 0o777 == 0o700
    assert b'fake-secret' not in (tmp_path / 'evidence/control/fault.marker').read_bytes()


def test_referenced_identity_check_prevents_unlink_of_replacement(tmp_path):
    files = ArtifactFiles(tmp_path)
    path, _, _ = artifact(files)
    candidate = files.candidates()[0]
    replacement = tmp_path / 'replacement'
    replacement.write_bytes(b'other')
    replacement.replace(tmp_path / path)
    with pytest.raises(BusinessError):
        files.unlink(path, candidate[3])
    assert (tmp_path / path).read_bytes() == b'other'


def test_unsafe_orphan_symlink_not_followed_or_deleted(tmp_path):
    files = ArtifactFiles(tmp_path)
    external = tmp_path / 'external'
    external.write_bytes(b'external')
    unsafe = tmp_path / 'evidence/blobs' / (uuid4().hex + '.blob')
    unsafe.symlink_to(external)
    assert files.candidates() == []
    assert external.read_bytes() == b'external'


def test_fault_latch_survives_rejected_inplace_update(tmp_path, monkeypatch):
    import errno
    files = ArtifactFiles(tmp_path)
    def full(*args):
        raise OSError(errno.ENOSPC, 'synthetic copy-on-write fault')
    monkeypatch.setattr(os, 'pwrite', full)
    files.latch_fault()
    with pytest.raises(BusinessError):
        files.assert_available()
    with pytest.raises(BusinessError):
        ArtifactFiles(tmp_path)


def test_concurrent_first_constructors_wait_for_allocated_marker_without_repair(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    for iteration in range(5):
        barrier = threading.Barrier(4)
        directory = tmp_path / str(iteration)
        def initialize(_):
            barrier.wait()
            files = ArtifactFiles(directory)
            try:
                files.assert_available()
                return (directory / 'evidence/control/fault.marker').read_bytes()
            finally:
                files.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            assert list(pool.map(initialize, range(4))) == [b'0' + b'\0' * 4095] * 4
