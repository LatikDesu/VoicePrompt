"""An expired/stale owner cannot extend or steal the active helper lease."""

import os

import pytest

from voiceprompt.ownership import Lease, OwnerBusy, OwnerLock, OwnershipError


def test_old_owner_cannot_keep_new_owner_alive():
    lease = Lease("new", now=100.0)
    assert not lease.renew("old", now=109.0)
    assert lease.expired(now=110.0)


def test_owner_renewal_extends_deadline():
    lease = Lease("g1", now=100.0)
    assert lease.renew("g1", now=109.0)
    assert not lease.expired(now=110.0)
    assert lease.expired(now=119.0)


def test_expired_lease_cannot_be_revived():
    lease = Lease("g1", now=100.0)
    assert not lease.renew("g1", now=110.0)
    assert lease.expired(now=110.0)


def test_second_owner_cannot_acquire_active_singleton(tmp_path):
    runtime = tmp_path / "runtime"
    first = OwnerLock.acquire(runtime, "g1")
    try:
        with pytest.raises(OwnerBusy):
            OwnerLock.acquire(runtime, "g2")
    finally:
        first.close()
    second = OwnerLock.acquire(runtime, "g2")
    try:
        first.close()
        with pytest.raises(OwnerBusy):
            OwnerLock.acquire(runtime, "g3")
    finally:
        second.close()


def test_insecure_existing_runtime_directory_is_rejected(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o755)
    os.chmod(runtime, 0o755)
    with pytest.raises(OwnershipError, match="insecure_runtime_directory"):
        OwnerLock.acquire(runtime, "g1")


def test_symlink_runtime_is_not_followed(tmp_path):
    target = tmp_path / "other"
    target.mkdir(mode=0o700)
    runtime = tmp_path / "runtime"
    runtime.symlink_to(target, target_is_directory=True)
    with pytest.raises(OwnershipError, match="insecure_runtime_directory"):
        OwnerLock.acquire(runtime, "g1")
