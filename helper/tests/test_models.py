import hashlib
import json
import os
from pathlib import Path

import pytest

from voiceprompt import models
from voiceprompt.model_download import PROFILES
from voiceprompt.models import ModelError, validate_local

PINNED_REPO, PINNED_REVISION = PROFILES["cpu-small"]
_BASE_REVISION = "b" * 40


def _model_dir(path: Path, *, repo: str | None = None, revision: str | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    contents = {"model.bin": b"ct2-fixture", "config.json": b'{"model_type":"Whisper"}',
                "tokenizer.json": b'{"model":{"vocab":{"<|ru|>":1}}}'}
    for name, data in contents.items():
        (path / name).write_bytes(data)
    if repo is not None:
        (path / ".voiceprompt-model.json").write_text(json.dumps({
            "schema": 1, "repo": repo, "revision": revision,
            "checksums": {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()},
        }), encoding="utf-8")
    return path


def _forbidden(name: str):
    def fail(*_args, **_kwargs):
        raise AssertionError(f"{name} must not run")
    return fail


def test_missing_tokenizer_is_rejected_without_runtime_download(tmp_path):
    (tmp_path / "model.bin").write_bytes(b"invalid-model-fixture")
    (tmp_path / "config.json").write_text('{"model_type":"Whisper"}')
    with pytest.raises(ModelError, match="tokenizer_missing"):
        validate_local(tmp_path)


def test_non_ct2_directory_is_rejected(tmp_path):
    (tmp_path / "weights.safetensors").write_bytes(b"not-ct2")
    with pytest.raises(ModelError, match="model_missing"):
        validate_local(tmp_path)


def test_vocabulary_does_not_allow_implicit_hub_tokenizer_fetch(tmp_path):
    (tmp_path / "model.bin").write_bytes(b"fixture")
    (tmp_path / "config.json").write_text('{"model_type":"Whisper"}')
    (tmp_path / "vocabulary.json").write_text('["hello"]')
    with pytest.raises(ModelError, match="tokenizer_missing"):
        validate_local(tmp_path)


def test_cuda_oom_is_not_reported_as_missing_cuda_runtime():
    from voiceprompt.backend import WhisperBackend
    assert WhisperBackend._load_error_code(RuntimeError("CUDA out of memory"), "cuda") == "inference_out_of_memory"


def test_ct2_whisper_config_without_invented_model_type_is_supported(tmp_path):
    (tmp_path / "model.bin").write_bytes(b"fixture")
    (tmp_path / "config.json").write_text('{"lang_ids":[50259,50260],"alignment_heads":[[5,3]],"suppress_ids":[1]}')
    (tmp_path / "tokenizer.json").write_text('{"model":{"vocab":{"<|ru|>":50263}}}')
    (tmp_path / "vocabulary.txt").write_text("hello\nworld\n")
    model = validate_local(tmp_path)
    assert model.local_path == tmp_path
    assert "vocabulary.txt" in model.checksum_manifest


def test_pinned_profile_rejects_the_same_repo_at_another_commit(tmp_path):
    path = _model_dir(tmp_path / "model", repo=PINNED_REPO, revision=_BASE_REVISION)
    ref = validate_local(path)
    assert (ref.repo, ref.revision) == (PINNED_REPO, _BASE_REVISION)
    with pytest.raises(ModelError, match="model_profile_mismatch"):
        models.check_profile(ref, "cpu-small")
    with pytest.raises(ModelError, match="model_profile_mismatch"):
        models.validate_profile(path, "cpu-small")


def test_pinned_profile_accepts_only_the_exact_pinned_identity(tmp_path):
    path = _model_dir(tmp_path / "model", repo=PINNED_REPO, revision=PINNED_REVISION)
    ref = models.validate_profile(path, "cpu-small")
    assert (ref.repo, ref.revision) == (PINNED_REPO, PINNED_REVISION)

    turbo_repo, turbo_revision = PROFILES["cuda-turbo"]
    turbo = _model_dir(tmp_path / "turbo", repo=turbo_repo, revision=turbo_revision)
    with pytest.raises(ModelError, match="model_profile_mismatch"):
        models.validate_profile(turbo, "cpu-small")
    assert models.validate_profile(turbo, "cuda-turbo").revision == turbo_revision


def test_pinned_profile_rejects_a_directory_without_hub_identity(tmp_path):
    with pytest.raises(ModelError, match="model_profile_mismatch"):
        models.validate_profile(_model_dir(tmp_path / "local"), "cpu-small")


def test_custom_profile_keeps_accepting_a_local_model_without_identity(tmp_path):
    ref = models.validate_profile(_model_dir(tmp_path / "custom"), "custom")
    assert ref.repo is None and ref.revision is None


def test_superseded_owned_revision_is_refreshed_to_the_current_pin(tmp_path, monkeypatch):
    target = _model_dir(tmp_path / "cpu-small", repo=PINNED_REPO, revision=_BASE_REVISION)

    def pinned_download(repo, revision, destination, progress=None, cancelled=None):
        assert (repo, revision) == (PINNED_REPO, PINNED_REVISION)
        return validate_local(_model_dir(Path(destination), repo=repo, revision=revision))

    monkeypatch.setattr(models, "download_model", pinned_download)
    ref = models.prepare_model("cpu-small", target)

    assert (ref.repo, ref.revision) == (PINNED_REPO, PINNED_REVISION)
    assert ref.local_path == target.resolve()
    assert models.validate_profile(target, "cpu-small").revision == PINNED_REVISION
    assert not list(tmp_path.glob(".cpu-small.repair-*"))
    # A repaired model is published by rename, so readiness must read it again: the proof
    # recorded for the staging tree is never claimed for the published path.
    assert not models.capture_validated(ref).matches(target)


def test_prepare_model_never_replaces_another_repository(tmp_path, monkeypatch):
    foreign = _model_dir(tmp_path / "foreign", repo="openai/whisper-small", revision=_BASE_REVISION)
    before = {item.name: item.read_bytes() for item in foreign.iterdir()}
    monkeypatch.setattr(models, "download_model", _forbidden("download_model"))

    with pytest.raises(ModelError, match="model_profile_mismatch"):
        models.prepare_model("cpu-small", foreign)
    assert {item.name: item.read_bytes() for item in foreign.iterdir()} == before


def test_captured_digests_skip_rehashing_the_same_operation(tmp_path, monkeypatch):
    path = _model_dir(tmp_path / "model")
    digests = {name: hashlib.sha256((path / name).read_bytes()).hexdigest()
               for name in ("model.bin", "config.json", "tokenizer.json")}
    with monkeypatch.context() as context:
        context.setattr(models, "_digest", _forbidden("_digest"))
        ref = models._validate_directory(path, None, None, digests=digests)
    assert ref.checksum_manifest == digests


def test_captured_digests_still_enforce_a_declared_manifest(tmp_path):
    path = _model_dir(tmp_path / "model", repo=PINNED_REPO, revision=PINNED_REVISION)
    with pytest.raises(ModelError, match="model_checksum_mismatch"):
        models._validate_directory(path, PINNED_REPO, PINNED_REVISION,
                                   digests={"model.bin": "0" * 64})


def test_captured_validation_is_reused_only_while_the_directory_is_unchanged(tmp_path):
    path = _model_dir(tmp_path / "model", repo=PINNED_REPO, revision=PINNED_REVISION)
    captured = models.capture_validated(models.validate_profile(path, "cpu-small"))
    assert captured.matches(path)
    assert not captured.matches(tmp_path / "elsewhere")
    (path / "model.bin").write_bytes(b"replaced-weights")
    assert not captured.matches(path)


def test_capture_is_invalidated_by_a_change_between_validation_and_capture(tmp_path):
    path = _model_dir(tmp_path / "model", repo=PINNED_REPO, revision=PINNED_REVISION)
    ref = models.validate_profile(path, "cpu-small")

    (path / "model.bin").write_bytes(b"changed-after-validation")

    captured = models.capture_validated(ref)
    assert not captured.matches(path)
    with pytest.raises(ModelError, match="model_checksum_mismatch"):
        models.validate_profile(path, "cpu-small")


def test_capture_is_invalidated_by_changed_weights_with_restored_mtime(tmp_path):
    path = _model_dir(tmp_path / "model", repo=PINNED_REPO, revision=PINNED_REVISION)
    ref = models.validate_profile(path, "cpu-small")
    target = path / "model.bin"
    original = target.stat()

    # Same size, same mtime, same inode: only ctime (nanosecond resolution on the supported
    # Linux filesystems) can expose the rewrite, so the proof must include it.
    target.write_bytes(b"ct2-REPLACE")
    os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert target.stat().st_size == original.st_size
    assert target.stat().st_mtime_ns == original.st_mtime_ns

    captured = models.capture_validated(ref)
    assert not captured.matches(path)
    with pytest.raises(ModelError, match="model_checksum_mismatch"):
        models.validate_profile(path, "cpu-small")


def test_capture_is_invalidated_by_a_permission_change(tmp_path):
    path = _model_dir(tmp_path / "model", repo=PINNED_REPO, revision=PINNED_REVISION)
    captured = models.capture_validated(models.validate_profile(path, "cpu-small"))
    assert captured.matches(path)

    os.chmod(path / "tokenizer.json", 0o400)
    assert not captured.matches(path)
    assert models.validate_profile(path, "cpu-small").revision == PINNED_REVISION


def test_validation_fails_when_the_tree_changes_while_it_is_read(tmp_path, monkeypatch):
    path = _model_dir(tmp_path / "model", repo=PINNED_REPO, revision=PINNED_REVISION)
    real_digest = models._digest

    def digest_then_change_permissions(target):
        value = real_digest(target)
        if target.name == "model.bin":
            os.chmod(path / "tokenizer.json", 0o600)
        return value

    monkeypatch.setattr(models, "_digest", digest_then_change_permissions)
    with pytest.raises(ModelError, match="model_unreadable"):
        validate_local(path)


def test_reference_without_a_validation_proof_is_never_reused(tmp_path):
    path = _model_dir(tmp_path / "model", repo=PINNED_REPO, revision=PINNED_REVISION)
    published = models.ModelRef(PINNED_REPO, PINNED_REVISION, path, {}, None)
    assert not models.capture_validated(published).matches(path)


@pytest.mark.parametrize("name", ["model.bin", "tokenizer.json", ".voiceprompt-model.json"])
def test_symlink_target_changes_invalidate_validation_proof(tmp_path, name):
    path = _model_dir(tmp_path / "model", repo=PINNED_REPO, revision=PINNED_REVISION)
    target = tmp_path / ("external-" + name)
    (path / name).replace(target)
    (path / name).symlink_to(target)
    captured = models.capture_validated(models.validate_profile(path, "cpu-small"))
    assert captured.matches(path), "unchanged symlinked models remain usable"
    before = target.stat()
    if name == "model.bin":
        target.write_bytes(b"ct2-REPLACE")
    elif name == "tokenizer.json":
        target.write_text('{"model":{"vocab":{"<|en|>":1}}}')
    else:
        value = json.loads(target.read_text())
        value["revision"] = _BASE_REVISION
        target.write_text(json.dumps(value))
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert target.stat().st_size == before.st_size
    assert not captured.matches(path), "a link's unchanged inode must not hide changed target bytes"
    code = "model_profile_mismatch" if name == ".voiceprompt-model.json" else "model_checksum_mismatch"
    with pytest.raises(ModelError, match=code):
        models.validate_profile(path, "cpu-small")


def test_symlink_target_change_during_hashing_rejects_ambiguous_proof(tmp_path, monkeypatch):
    path = _model_dir(tmp_path / "custom")
    target = tmp_path / "external-model.bin"
    (path / "model.bin").replace(target)
    (path / "model.bin").symlink_to(target)
    digest = models._digest

    def digest_then_change_target(asset):
        value = digest(asset)
        if asset.name == "model.bin":
            before = target.stat()
            target.write_bytes(b"ct2-REPLACE")
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        return value

    monkeypatch.setattr(models, "_digest", digest_then_change_target)
    with pytest.raises(ModelError, match="model_unreadable"):
        models.validate_profile(path, "custom")


def test_custom_symlinked_model_remains_valid_without_pinned_provenance(tmp_path):
    path = _model_dir(tmp_path / "custom")
    target = tmp_path / "external-model.bin"
    (path / "model.bin").replace(target)
    (path / "model.bin").symlink_to(target)
    ref = models.validate_profile(path, "custom")
    assert ref.repo is None and ref.revision is None
    assert ref.checksum_manifest["model.bin"] == hashlib.sha256(b"ct2-fixture").hexdigest()
    assert models.capture_validated(ref).matches(path)
