import json
from pathlib import Path

from test_supervisor import command, running_helper


def test_history_uses_private_staging_not_inline_command_text(tmp_path):
    runtime = tmp_path / "runtime"
    data_root = tmp_path / "data"
    with running_helper(runtime):
        allocation = command(runtime, "prepare_private_file", request_id="g1:r1",
                             payload={"purpose": "history", "max_bytes": 65536})
        assert allocation["accepted"] is True
        records = [{"session_id": "s1", "raw": "не удаляй 42 файла",
                    "current": "Не удаляй 42 файла.", "timestamp": 1000}]
        Path(allocation["path"]).write_text(json.dumps({"schema": 1, "records": records}))
        saved = command(runtime, "history_save", request_id="g1:r2",
                        payload={"root": str(data_root), "token": allocation["token"]})
        assert saved["accepted"] is True
        loaded = command(runtime, "history_load", request_id="g1:r3", payload={"root": str(data_root)})
        assert loaded["accepted"] is True
        assert "records" not in loaded
        assert json.loads(Path(loaded["path"]).read_text())["records"] == records
        assert Path(loaded["path"]).stat().st_mode & 0o777 == 0o600
        assert command(runtime, "discard_private_file", request_id="g1:r4", payload={"token": loaded["token"]})["accepted"]


def test_large_valid_history_is_not_limited_by_control_message_ceiling(tmp_path):
    runtime = tmp_path / "runtime"
    records = [{"session_id": f"s{i}", "raw": "x" * 65536, "current": "y" * 65536, "timestamp": 1000} for i in range(3)]
    with running_helper(runtime):
        allocated = command(runtime, "prepare_private_file", request_id="g1:r1",
                            payload={"purpose": "history", "max_bytes": 8388608})
        assert allocated["accepted"]
        Path(allocated["path"]).write_text(json.dumps({"schema": 1, "records": records}))
        assert command(runtime, "history_save", request_id="g1:r2",
                       payload={"root": str(tmp_path / "data"), "token": allocated["token"]})["accepted"]
        loaded = command(runtime, "history_load", request_id="g1:r3", payload={"root": str(tmp_path / "data")})
        assert loaded["accepted"]
        assert json.loads(Path(loaded["path"]).read_text())["records"] == records

