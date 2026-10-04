"""Private staging tokens and bounded history storage."""

import json
import math
import os
import stat
import tempfile
import time
import uuid
from pathlib import Path


class DataError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _private_directory(root: Path) -> Path:
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise DataError("private_directory_invalid")
    return root


def _read_regular(path: Path, maximum: int, code="private_file_invalid") -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise DataError(code)
            if info.st_size > maximum:
                raise DataError("staging_too_large" if code == "staging_invalid" else "data_too_large")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                content = stream.read(maximum + 1)
            if len(content) > maximum:
                raise DataError("data_too_large")
            return content
        finally:
            os.close(fd)
    except DataError:
        raise
    except OSError:
        raise DataError(code) from None


def atomic_private_write(path: Path, content: bytes) -> None:
    path = Path(path)
    root = _private_directory(path.parent)
    fd, name = tempfile.mkstemp(prefix=".voiceprompt-", dir=root)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        raise DataError("data_write_failed") from None
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


class PrivateFiles:
    def __init__(self, root: Path) -> None:
        self.root = _private_directory(root)
        self.allocations = {}

    def allocate(self, purpose: str, max_bytes: int) -> dict:
        self.expire()
        limits = {"history": 8388608, "probe": 65536}
        if purpose not in limits or type(max_bytes) is not int or not 0 < max_bytes <= limits[purpose]:
            raise DataError("staging_limit_invalid")
        if len(self.allocations) >= 4:
            raise DataError("staging_busy")
        token = uuid.uuid4().hex
        directory = self.root / token
        directory.mkdir(mode=0o700)
        fd, name = tempfile.mkstemp(prefix="input-", dir=directory)
        os.fchmod(fd, 0o600)
        os.close(fd)
        self.allocations[token] = {"path": Path(name), "maximum": max_bytes,
                                   "expires": time.monotonic() + 30}
        return {"path": name, "token": token}

    def consume(self, token: str) -> bytes:
        self.expire()
        item = self.allocations.get(token)
        if item is None:
            raise DataError("staging_token_invalid")
        try:
            return _read_regular(item["path"], item["maximum"], "staging_invalid")
        finally:
            self.discard(token)

    def discard(self, token: str) -> None:
        item = self.allocations.pop(token, None)
        if item is None:
            return
        try:
            item["path"].unlink(missing_ok=True)
            item["path"].parent.rmdir()
        except OSError:
            raise DataError("staging_cleanup_failed") from None

    def expire(self) -> None:
        now = time.monotonic()
        for token, item in list(self.allocations.items()):
            if now >= item["expires"]:
                self.discard(token)

    def close(self) -> None:
        for token in list(self.allocations):
            self.discard(token)


class HistoryStore:
    def __init__(self, root: Path, schema: int = 1) -> None:
        self.root = _private_directory(root)
        self.schema = schema
        self.path = self.root / "history.json"

    @staticmethod
    def _records(records: list[dict]) -> list[dict]:
        if not isinstance(records, list):
            raise DataError("history_malformed")
        accepted = []
        seen = set()
        for record in records:
            if not isinstance(record, dict) or set(record) != {"session_id", "raw", "current", "timestamp"}:
                raise DataError("history_malformed")
            identifier = record["session_id"]
            if not isinstance(identifier, str) or not identifier or len(identifier.encode()) > 256 or identifier in seen:
                raise DataError("history_malformed")
            seen.add(identifier)
            if type(record["timestamp"]) not in {int, float} or not math.isfinite(record["timestamp"]):
                raise DataError("history_malformed")
            for key in ("raw", "current"):
                if not isinstance(record[key], str) or len(record[key].encode("utf-8")) > 65536:
                    raise DataError("history_text_limit")
            accepted.append(dict(record))
        return accepted[-10:]

    def save(self, records: list[dict]) -> None:
        if self.path.exists():
            self.load()
        self._write(records)

    def _write(self, records: list[dict]) -> None:
        checked = self._records(records)
        content = json.dumps({"schema": self.schema, "records": checked}, ensure_ascii=False,
                             allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(content) > 8388608:
            raise DataError("history_too_large")
        atomic_private_write(self.path, content)

    def load(self) -> list[dict]:
        if not self.path.exists() and not self.path.is_symlink():
            return []
        content = _read_regular(self.path, 8388608, "history_insecure")
        try:
            value = json.loads(content)
            if not isinstance(value, dict) or type(value.get("schema")) is not int:
                raise DataError("history_malformed")
            if value["schema"] != self.schema:
                raise DataError("schema_incompatible")
            return self._records(value.get("records"))
        except (ValueError, UnicodeError) as error:
            if isinstance(error, DataError):
                raise
            raise DataError("history_malformed") from None

    def delete_record(self, session_id: str) -> None:
        self._write([item for item in self.load() if item["session_id"] != session_id])

    def clear(self) -> None:
        self.load()
        self._write([])

