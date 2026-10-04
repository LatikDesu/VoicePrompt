import json
import os

import pytest

from voiceprompt.private_files import DataError, HistoryStore, PrivateFiles


def record(identifier="s1", text="не удаляй 42 файла"):
    return {"session_id": identifier, "raw": text, "current": text, "timestamp": 1000}


@pytest.mark.parametrize("operation", ["load", "save", "delete_record", "clear"])
def test_newer_history_schema_is_not_silently_overwritten(tmp_path, operation):
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    path = root / "history.json"
    path.write_text(json.dumps({"schema": 2, "records": []}))
    os.chmod(path, 0o600)
    original = path.read_bytes()
    store = HistoryStore(root)
    with pytest.raises(DataError, match="schema_incompatible"):
        if operation == "save":
            store.save([record()])
        elif operation == "delete_record":
            store.delete_record("s1")
        else:
            getattr(store, operation)()
    assert path.read_bytes() == original


def test_atomic_failure_preserves_previous_complete_history(tmp_path, monkeypatch):
    store = HistoryStore(tmp_path / "data")
    store.save([record()])

    def interrupted(*args):
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr(os, "replace", interrupted)
    with pytest.raises(DataError):
        store.save([record(text="изменённый")])
    assert store.load() == [record()]


def test_history_caps_records_and_deletes_only_selected_session(tmp_path):
    store = HistoryStore(tmp_path / "data")
    store.save([record(f"s{i}") for i in range(12)])
    assert [item["session_id"] for item in store.load()] == [f"s{i}" for i in range(2, 12)]
    store.delete_record("s5")
    assert [item["session_id"] for item in store.load()] == ["s2", "s3", "s4", "s6", "s7", "s8", "s9", "s10", "s11"]
    assert (store.root / "history.json").stat().st_mode & 0o777 == 0o600
    assert store.root.stat().st_mode & 0o777 == 0o700


def test_staging_symlink_cannot_read_foreign_file(tmp_path):
    private = PrivateFiles(tmp_path / "staging")
    allocation = private.allocate("history", 65536)
    path = allocation["path"]
    from pathlib import Path
    path = Path(path)
    secret = tmp_path / "unrelated"
    secret.write_text("must not be consumed")
    path.unlink()
    path.symlink_to(secret)
    with pytest.raises(DataError, match="staging_invalid"):
        private.consume(allocation["token"])
    assert secret.read_text() == "must not be consumed"


def test_oversized_staging_content_is_rejected_without_echo(tmp_path):
    private = PrivateFiles(tmp_path / "staging")
    allocation = private.allocate("probe", 4)
    from pathlib import Path
    Path(allocation["path"]).write_text("too long")
    with pytest.raises(DataError, match="staging_too_large"):
        private.consume(allocation["token"])

