"""An unusable STT import must not be reported as a working CPU runtime."""

from voiceprompt import diagnostics


def test_broken_stt_import_prevents_cpu_runtime_advertisement(monkeypatch):
    real_import = diagnostics.importlib.import_module

    def unavailable_stt(name):
        if name == "faster_whisper":
            raise ImportError("dependency cannot load")
        return real_import(name)

    monkeypatch.setattr(diagnostics.importlib, "import_module", unavailable_stt)
    report = diagnostics.inspect_cpu()
    assert report["cpu"]["runtime_available"] is False
    assert report["cpu"]["error_code"] == "cpu_runtime_unavailable"


def test_missing_cuda_libraries_remain_visible_in_child_probe(monkeypatch):
    import json
    from types import SimpleNamespace
    report = {'runtime_available': False, 'missing_libraries': ['libcudnn.so.9'],
              'error_code': 'cuda_libraries_missing', 'compute_types': [], 'gpu_count': 0}
    monkeypatch.setattr(diagnostics.subprocess, 'run', lambda *args, **kwargs:
                        SimpleNamespace(returncode=1, stdout=json.dumps(report), stderr=''))
    observed = diagnostics.inspect_cuda()
    assert observed['runtime_available'] is False
    assert observed['missing_libraries'] == ['libcudnn.so.9']
    assert observed['error_code'] == 'cuda_libraries_missing'


def test_invalid_cuda_diagnostics_never_advertise_gpu_available(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(diagnostics.subprocess, 'run', lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout='not JSON', stderr=''))
    observed = diagnostics.inspect_cuda()
    assert observed['runtime_available'] is False
    assert observed['error_code'] == 'cuda_diagnostics_invalid'


def test_missing_production_vad_graph_prevents_readiness(monkeypatch, tmp_path):
    import importlib.metadata

    real_distribution = importlib.metadata.distribution
    distribution = real_distribution("faster-whisper")
    real_locate = distribution.locate_file

    def locate_without_vad(path):
        if str(path) == "faster_whisper/assets/silero_vad_v6.onnx":
            return tmp_path / "missing-vad.onnx"
        return real_locate(path)

    monkeypatch.setattr(distribution, "locate_file", locate_without_vad)
    monkeypatch.setattr(importlib.metadata, "distribution",
                        lambda name: distribution if name == "faster-whisper" else real_distribution(name))
    report = diagnostics.inspect_cpu()
    assert "CPUExecutionProvider" in report["vad"]["providers"]
    assert report["vad"]["runtime_available"] is False
    assert report["vad"]["error_code"] == "vad_runtime_unavailable"


def _pinned_model(path):
    import hashlib
    import json
    from voiceprompt.model_download import PROFILES

    repo, revision = PROFILES["cpu-small"]
    path.mkdir(parents=True, exist_ok=True)
    contents = {"model.bin": b"ct2-fixture", "config.json": b'{"model_type":"Whisper"}',
                "tokenizer.json": b'{"model":{"vocab":{"<|ru|>":1}}}'}
    for name, data in contents.items():
        (path / name).write_bytes(data)
    (path / ".voiceprompt-model.json").write_text(json.dumps({
        "schema": 1, "repo": repo, "revision": revision,
        "checksums": {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()},
    }), encoding="utf-8")
    return path


def test_model_state_reuses_an_unchanged_capture_and_revalidates_changes(tmp_path, monkeypatch):
    from voiceprompt import models

    path = _pinned_model(tmp_path / "cpu-small")
    captured = models.capture_validated(models.validate_profile(path, "cpu-small"))
    real_validate = models.validate_profile
    validations = []
    monkeypatch.setattr(models, "validate_profile",
                        lambda target, profile: (validations.append(str(target)), real_validate(target, profile))[1])

    assert diagnostics.model_state(path, "cpu-small", captured) == {"available": True, "error_code": None}
    assert validations == []

    (path / "model.bin").write_bytes(b"changed-weights")
    assert diagnostics.model_state(path, "cpu-small", captured) == {
        "available": False, "error_code": "model_checksum_mismatch"}
    assert validations == [str(path)]


def test_model_state_ignores_a_capture_taken_for_another_profile(tmp_path):
    from voiceprompt import models

    path = _pinned_model(tmp_path / "cpu-small")
    captured = models.capture_validated(models.validate_profile(path, "cpu-small"))

    assert diagnostics.model_state(path, "cpu-small", captured) == {"available": True, "error_code": None}
    assert diagnostics.model_state(path, "cuda-turbo", captured) == {
        "available": False, "error_code": "model_profile_mismatch"}


def test_model_state_rejects_a_capture_whose_proof_predates_a_change(tmp_path):
    from voiceprompt import models

    path = _pinned_model(tmp_path / "cpu-small")
    ref = models.validate_profile(path, "cpu-small")
    (path / "model.bin").write_bytes(b"changed-weights")

    captured = models.capture_validated(ref)

    assert diagnostics.model_state(path, "cpu-small", captured) == {
        "available": False, "error_code": "model_checksum_mismatch"}
