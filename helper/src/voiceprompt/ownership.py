"""Private runtime ownership. Keep the lock inode linked across reloads."""

import fcntl
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path


class OwnershipError(RuntimeError):
    pass


class OwnerBusy(OwnershipError):
    pass


@dataclass(slots=True)
class Lease:
    owner: str
    now: float
    ttl: float = 10.0

    def renew(self, owner: str, now: float) -> bool:
        if owner != self.owner or self.expired(now):
            return False
        self.now = now
        return True

    def expired(self, now: float) -> bool:
        return now >= self.now + self.ttl


class OwnerLock:
    def __init__(self, runtime_dir: Path, owner: str, fd: int) -> None:
        self.runtime_dir = runtime_dir
        self.owner = owner
        self.fd: int | None = fd

    @classmethod
    def acquire(cls, runtime_dir: Path, owner: str) -> "OwnerLock":
        runtime_dir = Path(runtime_dir)
        if not isinstance(owner, str) or not owner or len(owner.encode()) > 256:
            raise OwnershipError("invalid_owner")
        try:
            runtime_dir.mkdir(mode=0o700, exist_ok=True)
            info = runtime_dir.lstat()
        except OSError:
            raise OwnershipError("runtime_directory_unavailable") from None
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise OwnershipError("insecure_runtime_directory")
        try:
            fd = os.open(runtime_dir / "owner.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        except OSError:
            raise OwnershipError("lock_unavailable") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise OwnershipError("insecure_lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise OwnerBusy("owner_busy") from None
            metadata = json.dumps({"pid": os.getpid(), "owner_generation": owner}).encode()
            os.ftruncate(fd, 0)
            os.write(fd, metadata)
            return cls(runtime_dir, owner, fd)
        except BaseException:
            os.close(fd)
            raise

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
