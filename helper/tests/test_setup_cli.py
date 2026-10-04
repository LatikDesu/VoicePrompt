"""Deterministic setup/setup-status behavior: reuse, repair, failure preservation.

These tests never touch the network, a real model, a microphone or PipeWire. The
runtime/device probes and the model download/load seams are monkeypatched so the
CLI's own decisions (reuse, repair, refusal, staging preservation) are the subject.
"""

import json
import hashlib
from pathlib import Path

import pytest

from voiceprompt import cli, diagnostics, models
from voiceprompt.model_download import PROFILES

_TOKENIZER = '{"model":{"vocab":{"<|ru|>":1}}}'
_PINNED_REPO, _PINNED_REVISION = PROFILES["cpu-small"]


def _valid_model(path: Path, *, repo: str = _PINNED_REPO, revision: str = _PINNED_REVISION) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "model.bin").write_bytes(b"ct2-fixture")
    (path / "config.json").write_text('{"model_type":"Whisper"}', encoding="utf-8")
    (path / "tokenizer.json").write_text(_TOKENIZER, encoding="utf-8")
    (path / ".voiceprompt-model.json").write_text(json.dumps({
        "schema": 1, "repo": repo, "revision": revision,
        "checksums": {name: hashlib.sha256((path / name).read_bytes()).hexdigest()
                      for name in ("model.bin", "config.json", "tokenizer.json")},
    }), encoding="utf-8")
    return path


def _cpu_report(*, cpu: bool = True, vad: bool = True, pipewire: str = "ok") -> dict:
    if pipewire == "ok":
        pipewire_report = {"executable": "/usr/bin/pw-record", "version": "pw-record 1.0",
                           "server_reachable": True}
    elif pipewire == "unreachable":
        pipewire_report = {"executable": "/usr/bin/pw-record", "version": "pw-record 1.0",
                           "server_reachable": False}
    elif pipewire == "unverified":
        pipewire_report = {"executable": "/usr/bin/pw-record", "version": "pw-record 1.0",
                           "server_reachable": None}
    else:
        pipewire_report = {"executable": None, "version": None, "server_reachable": None}
    return {
        "python": "3.13.0",
        "platform": {"system": "Linux", "architecture": "x86_64"},
        "packages": {},
        "cpu": {
            "runtime_available": cpu,
            "compute_types": ["int8"] if cpu else [],
            "instruction_features": [],
            **({} if cpu else {"error_code": "cpu_runtime_unavailable"}),
        },
        "vad": {
            "runtime_available": vad,
            "providers": ["CPUExecutionProvider"] if vad else [],
            **({} if vad else {"error_code": "vad_runtime_unavailable"}),
        },
        "pipewire": pipewire_report,
        "model": {"loaded": False},
        "optional": {"cuda": "not_probed", "evdev": "not_probed"},
    }


def _forbidden(name: str):
    def fail(*_args, **_kwargs):
        raise AssertionError(f"{name} must not run")
    return fail


def test_setup_reuses_valid_model_without_download(tmp_path, monkeypatch, capsys):
    model = _valid_model(tmp_path / "cpu-small")
    monkeypatch.setattr(models, "download_model", _forbidden("download_model"))
    monkeypatch.setattr(models, "smoke_load",
                        lambda path, device: {"loaded": True, "error_code": None})
    monkeypatch.setattr(diagnostics, "inspect_cpu", lambda: _cpu_report())

    code = cli.main(["setup", "--device", "cpu", "--model-profile", "cpu-small",
                     "--model-path", str(model), "--json"])
    out = json.loads(capsys.readouterr().out)

    assert code == 0
    assert out["selected_device"] == "cpu"
    assert out["model_profile"] == "cpu-small"
    assert out["model"] == {"available": True, "error_code": None}
    assert out["model_load"] == {"loaded": True, "error_code": None}
    assert out["recording_ready"] is True
    assert out["error_codes"] == []


def test_missing_standard_model_is_downloaded_to_target(tmp_path, monkeypatch, capsys):
    target = tmp_path / "cache" / "cpu-small"
    calls = []

    def downloading(repo, revision, destination, **_kwargs):
        calls.append((repo, revision, Path(destination)))
        assert Path(destination) == target
        return models.validate_local(_valid_model(Path(destination)))

    monkeypatch.setattr(models, "download_model", downloading)
    monkeypatch.setattr(models, "smoke_load",
                        lambda path, device: {"loaded": True, "error_code": None})
    monkeypatch.setattr(diagnostics, "inspect_cpu", lambda: _cpu_report())

    code = cli.main(["setup", "--device", "cpu", "--model-profile", "cpu-small",
                     "--model-path", str(target), "--json"])
    out = json.loads(capsys.readouterr().out)

    assert code == 0
    assert calls == [("Systran/faster-whisper-small",
                      "536b0662742c02347bc0e980a01041f333bce120", target)]
    assert out["model"] == {"available": True, "error_code": None}


def test_corrupt_standard_model_is_repaired_by_staged_download(tmp_path, monkeypatch, capsys):
    cache = tmp_path / "cache"
    target = _valid_model(cache / "cpu-small")
    (target / "tokenizer.json").write_text("not json", encoding="utf-8")
    calls = []

    def repairing(repo, revision, destination, **_kwargs):
        destination = Path(destination)
        calls.append((repo, revision, destination))
        assert destination != target
        assert not destination.exists()
        return models.validate_local(_valid_model(destination))

    monkeypatch.setattr(models, "download_model", repairing)
    monkeypatch.setattr(models, "smoke_load",
                        lambda path, device: {"loaded": True, "error_code": None})
    monkeypatch.setattr(diagnostics, "inspect_cpu", lambda: _cpu_report())

    code = cli.main(["setup", "--device", "cpu", "--model-profile", "cpu-small",
                     "--model-path", str(target), "--json"])
    out = json.loads(capsys.readouterr().out)

    assert code == 0
    assert calls and calls[0][0] == "Systran/faster-whisper-small"
    assert out["model"] == {"available": True, "error_code": None}
    assert (target / "tokenizer.json").read_text(encoding="utf-8") == _TOKENIZER
    assert not list(cache.glob(".cpu-small.repair-*"))


def test_failed_repair_keeps_prior_corrupt_artifact(tmp_path, monkeypatch, capsys):
    cache = tmp_path / "cache"
    target = _valid_model(cache / "cpu-small")
    (target / "tokenizer.json").write_text("not json", encoding="utf-8")
    before = {item.name: item.read_bytes() for item in target.iterdir()}

    monkeypatch.setattr(models, "download_model",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            models.ModelError("model_download_failed")))
    monkeypatch.setattr(models, "smoke_load", _forbidden("smoke_load"))
    monkeypatch.setattr(diagnostics, "inspect_cpu", lambda: _cpu_report())

    code = cli.main(["setup", "--device", "cpu", "--model-profile", "cpu-small",
                     "--model-path", str(target), "--json"])
    out = json.loads(capsys.readouterr().out)

    assert code == 1
    assert out["model"] == {"available": False, "error_code": "model_corrupt"}
    assert out["model_load"] == {"loaded": False, "error_code": "model_download_failed"}
    assert out["recording_ready"] is False
    assert "model_corrupt" in out["error_codes"]
    assert {item.name: item.read_bytes() for item in target.iterdir()} == before
    assert not list(cache.glob(".cpu-small.repair-*"))


def test_custom_invalid_path_is_never_downloaded_or_replaced(tmp_path, monkeypatch, capsys):
    target = _valid_model(tmp_path / "custom")
    (target / "tokenizer.json").write_text("not json", encoding="utf-8")
    before = {item.name: item.read_bytes() for item in target.iterdir()}
    monkeypatch.setattr(models, "download_model", _forbidden("download_model"))
    monkeypatch.setattr(models, "smoke_load", _forbidden("smoke_load"))
    monkeypatch.setattr(diagnostics, "inspect_cpu", lambda: _cpu_report())

    code = cli.main(["setup", "--device", "cpu", "--model-profile", "custom",
                     "--model-path", str(target), "--json"])
    out = json.loads(capsys.readouterr().out)

    assert code == 1
    assert out["model"] == {"available": False, "error_code": "model_corrupt"}
    assert {item.name: item.read_bytes() for item in target.iterdir()} == before


def test_failed_plain_download_preserves_foreign_staging(tmp_path, monkeypatch, capsys):
    cache = tmp_path / "cache"
    target = cache / "cpu-small"
    foreign = cache / ".cpu-small.voiceprompt-old"
    foreign.mkdir(parents=True)
    (foreign / "model.bin").write_bytes(b"other")

    monkeypatch.setattr(models, "download_model",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            models.ModelError("model_download_failed")))
    monkeypatch.setattr(models, "smoke_load", _forbidden("smoke_load"))
    monkeypatch.setattr(diagnostics, "inspect_cpu", lambda: _cpu_report())

    code = cli.main(["setup", "--device", "cpu", "--model-profile", "cpu-small",
                     "--model-path", str(target), "--json"])
    out = json.loads(capsys.readouterr().out)

    assert code == 1
    assert out["model"] == {"available": False, "error_code": "model_missing"}
    assert out["model_load"] == {"loaded": False, "error_code": "model_download_failed"}
    assert not target.exists()
    assert (foreign / "model.bin").read_bytes() == b"other"


def test_setup_status_never_downloads_or_loads_model(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(models, "download_model", _forbidden("download_model"))
    monkeypatch.setattr(models, "smoke_load", _forbidden("smoke_load"))
    monkeypatch.setattr(diagnostics, "inspect_cpu", lambda: _cpu_report())

    missing = tmp_path / "cache" / "cpu-small"
    code = cli.main(["setup-status", "--device", "cpu", "--model-profile", "cpu-small",
                     "--model-path", str(missing), "--json"])
    out = json.loads(capsys.readouterr().out)

    assert code == 1
    assert out["model"] == {"available": False, "error_code": "model_missing"}
    assert out["recording_ready"] is False
    assert "model_missing" in out["error_codes"]
    assert "model_load" not in out
    assert not missing.exists()


def test_setup_status_reports_each_unmet_condition(tmp_path, monkeypatch, capsys):
    model = _valid_model(tmp_path / "model")
    monkeypatch.setattr(diagnostics, "inspect_cpu",
                        lambda: _cpu_report(cpu=False, vad=False, pipewire="unreachable"))

    code = cli.main(["setup-status", "--device", "cpu", "--model-profile", "cpu-small",
                     "--model-path", str(model), "--json"])
    out = json.loads(capsys.readouterr().out)

    assert code == 1
    assert out["recording_ready"] is False
    assert {"cpu_runtime_unavailable", "vad_runtime_unavailable",
            "pipewire_unreachable"}.issubset(set(out["error_codes"]))


def test_unverified_pipewire_reachability_is_not_ready(tmp_path, monkeypatch, capsys):
    model = _valid_model(tmp_path / "model")
    monkeypatch.setattr(diagnostics, "inspect_cpu",
                        lambda: _cpu_report(pipewire="unverified"))

    code = cli.main(["setup-status", "--device", "cpu", "--model-profile", "cpu-small",
                     "--model-path", str(model), "--json"])
    out = json.loads(capsys.readouterr().out)

    assert code == 1
    assert out["recording_ready"] is False
    assert "pipewire_unverified" in out["error_codes"]


def test_selected_cuda_requires_cuda_runtime(tmp_path, monkeypatch, capsys):
    model = _valid_model(tmp_path / "model")
    monkeypatch.setattr(diagnostics, "inspect_cpu", lambda: _cpu_report())
    monkeypatch.setattr(diagnostics, "inspect_cuda",
                        lambda: {"runtime_available": False, "error_code": "cuda_libraries_missing"})

    code = cli.main(["setup-status", "--device", "cuda", "--model-profile", "cuda-turbo",
                     "--model-path", str(model), "--json"])
    out = json.loads(capsys.readouterr().out)

    assert code == 1
    assert out["recording_ready"] is False
    assert "cuda_libraries_missing" in out["error_codes"]


def test_default_model_path_uses_xdg_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert cli.default_model_path("cpu-small") == (
        tmp_path / "cache" / "voiceprompt" / "models" / "cpu-small")


def test_custom_profile_requires_explicit_model_path():
    with pytest.raises(SystemExit) as raised:
        cli.main(["setup-status", "--device", "cpu", "--model-profile", "custom", "--json"])
    assert raised.value.code == 2


def test_device_profile_mismatch_is_rejected():
    with pytest.raises(SystemExit) as raised:
        cli.main(["setup", "--device", "cuda", "--model-profile", "cpu-small", "--json"])
    assert raised.value.code == 2



def test_setup_does_not_replace_foreign_invalid_model(tmp_path, monkeypatch, capsys):
    target = _valid_model(tmp_path / "foreign")
    (target / ".voiceprompt-model.json").unlink()
    (target / "tokenizer.json").write_text("not json", encoding="utf-8")
    before = {item.name: item.read_bytes() for item in target.iterdir()}
    monkeypatch.setattr(models, "download_model", _forbidden("download_model"))
    monkeypatch.setattr(models, "smoke_load", _forbidden("smoke_load"))
    monkeypatch.setattr(diagnostics, "inspect_cpu", lambda: _cpu_report())
    code = cli.main(["setup", "--model-path", str(target), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["model_load"]["loaded"] is False
    assert {item.name: item.read_bytes() for item in target.iterdir()} == before


def test_setup_status_rejects_a_model_for_another_profile(tmp_path, monkeypatch, capsys):
    target = _valid_model(tmp_path / "cpu-small")
    monkeypatch.setattr(diagnostics, "inspect_cpu", lambda: _cpu_report())
    monkeypatch.setattr(diagnostics, "inspect_cuda", lambda: {"runtime_available": True})
    code = cli.main(["setup-status", "--device", "cuda", "--model-profile", "cuda-turbo",
                     "--model-path", str(target), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["recording_ready"] is False
    assert "model_profile_mismatch" in out["error_codes"]


def test_failed_model_load_is_not_reported_ready(tmp_path, monkeypatch, capsys):
    target = _valid_model(tmp_path / "cpu-small")
    monkeypatch.setattr(diagnostics, "inspect_cpu", lambda: _cpu_report())
    monkeypatch.setattr(models, "smoke_load", lambda *_: {"loaded": False, "error_code": "model_load_failed"})
    code = cli.main(["setup", "--model-path", str(target), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["recording_ready"] is False
    assert "model_load_failed" in out["error_codes"]


def test_setup_status_never_accepts_another_commit_for_a_pinned_profile(tmp_path, monkeypatch, capsys):
    model = _valid_model(tmp_path / "cpu-small", revision="b" * 40)
    monkeypatch.setattr(models, "download_model", _forbidden("download_model"))
    monkeypatch.setattr(models, "smoke_load", _forbidden("smoke_load"))
    monkeypatch.setattr(diagnostics, "inspect_cpu", lambda: _cpu_report())

    code = cli.main(["setup-status", "--device", "cpu", "--model-profile", "cpu-small",
                     "--model-path", str(model), "--json"])
    out = json.loads(capsys.readouterr().out)

    assert code == 1
    assert out["model"] == {"available": False, "error_code": "model_profile_mismatch"}
    assert out["recording_ready"] is False
    assert "model_profile_mismatch" in out["error_codes"]


def test_setup_refreshes_an_owned_model_pinned_to_a_superseded_commit(tmp_path, monkeypatch, capsys):
    cache = tmp_path / "cache"
    target = _valid_model(cache / "cpu-small", revision="b" * 40)
    calls = []

    def refreshing(repo, revision, destination, **_kwargs):
        calls.append((repo, revision, Path(destination)))
        return models.validate_local(_valid_model(Path(destination), repo=repo, revision=revision))

    monkeypatch.setattr(models, "download_model", refreshing)
    monkeypatch.setattr(models, "smoke_load",
                        lambda path, device: {"loaded": True, "error_code": None})
    monkeypatch.setattr(diagnostics, "inspect_cpu", lambda: _cpu_report())

    code = cli.main(["setup", "--device", "cpu", "--model-profile", "cpu-small",
                     "--model-path", str(target), "--json"])
    out = json.loads(capsys.readouterr().out)

    assert code == 0
    assert len(calls) == 1
    assert calls[0][:2] == (_PINNED_REPO, _PINNED_REVISION)
    assert calls[0][2] != target
    assert models.validate_local(target).revision == _PINNED_REVISION
    assert out["model"] == {"available": True, "error_code": None}
    assert not list(cache.glob(".cpu-small.repair-*"))


def test_setup_reuses_captured_validation_instead_of_revalidating(tmp_path, monkeypatch, capsys):
    model = _valid_model(tmp_path / "cpu-small")
    real_validate = models.validate_profile
    validations = []

    def counting(path, profile):
        validations.append(str(path))
        return real_validate(path, profile)

    monkeypatch.setattr(models, "validate_profile", counting)
    monkeypatch.setattr(models, "smoke_load",
                        lambda path, device: {"loaded": True, "error_code": None})
    monkeypatch.setattr(diagnostics, "inspect_cpu", lambda: _cpu_report())

    code = cli.main(["setup", "--device", "cpu", "--model-profile", "cpu-small",
                     "--model-path", str(model), "--json"])
    out = json.loads(capsys.readouterr().out)

    assert code == 0
    assert out["model"] == {"available": True, "error_code": None}
    assert validations == [str(model)]


def test_setup_revalidates_instead_of_reusing_a_capture_for_a_changed_model(tmp_path, monkeypatch, capsys):
    model = _valid_model(tmp_path / "cpu-small")

    def load_after_corruption(path, device):
        (Path(path) / "tokenizer.json").write_text("not json", encoding="utf-8")
        return {"loaded": True, "error_code": None}

    monkeypatch.setattr(models, "download_model", _forbidden("download_model"))
    monkeypatch.setattr(models, "smoke_load", load_after_corruption)
    monkeypatch.setattr(diagnostics, "inspect_cpu", lambda: _cpu_report())

    code = cli.main(["setup", "--device", "cpu", "--model-profile", "cpu-small",
                     "--model-path", str(model), "--json"])
    out = json.loads(capsys.readouterr().out)

    assert code == 1
    assert out["model"] == {"available": False, "error_code": "model_corrupt"}
    assert out["model_load"] == {"loaded": True, "error_code": None}
    assert out["recording_ready"] is False
    assert "model_corrupt" in out["error_codes"]


def test_custom_profile_accepts_a_local_model_without_pinned_identity(tmp_path, monkeypatch, capsys):
    model = _valid_model(tmp_path / "custom")
    (model / ".voiceprompt-model.json").unlink()
    monkeypatch.setattr(models, "download_model", _forbidden("download_model"))
    monkeypatch.setattr(models, "smoke_load",
                        lambda path, device: {"loaded": True, "error_code": None})
    monkeypatch.setattr(diagnostics, "inspect_cpu", lambda: _cpu_report())

    code = cli.main(["setup", "--device", "cpu", "--model-profile", "custom",
                     "--model-path", str(model), "--json"])
    out = json.loads(capsys.readouterr().out)

    assert code == 0
    assert out["model"] == {"available": True, "error_code": None}
    assert out["recording_ready"] is True
