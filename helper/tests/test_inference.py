import asyncio
import hashlib
import json
import time
from types import SimpleNamespace

import pytest

from voiceprompt.backend import WhisperBackend
from voiceprompt.models import ModelError, download_model, validate_local
from voiceprompt.worker import Worker


def _model_dir(path):
    path.mkdir()
    (path / "model.bin").write_bytes(b"fixture-weights")
    (path / "config.json").write_text('{"model_type":"Whisper"}', encoding="utf-8")
    (path / "tokenizer.json").write_text(
        json.dumps({"model": {"vocab": {"<|en|>": 1, "<|ru|>": 2}}}), encoding="utf-8"
    )
    return path


def test_local_model_ref_hashes_assets_without_requiring_hub_identity(tmp_path):
    path = _model_dir(tmp_path / "custom")
    model = validate_local(path)
    assert model.repo is None and model.revision is None
    assert model.checksum_manifest["model.bin"] == hashlib.sha256(b"fixture-weights").hexdigest()


def test_stored_checksum_manifest_rejects_changed_weights(tmp_path):
    path = _model_dir(tmp_path / "downloaded")
    checksums = {
        name: hashlib.sha256((path / name).read_bytes()).hexdigest()
        for name in ("model.bin", "config.json", "tokenizer.json")
    }
    (path / ".voiceprompt-model.json").write_text(json.dumps({
        "schema": 1, "repo": "Systran/faster-whisper-small",
        "revision": "a" * 40, "checksums": checksums,
    }), encoding="utf-8")
    (path / "model.bin").write_bytes(b"changed-weights")
    with pytest.raises(ModelError, match="model_checksum_mismatch"):
        validate_local(path)


def test_en_only_tokenizer_is_rejected_for_russian(tmp_path):
    path = _model_dir(tmp_path / "english")
    (path / "tokenizer.json").write_text(
        json.dumps({"model": {"vocab": {"<|en|>": 1}}}), encoding="utf-8"
    )
    model = validate_local(path)
    from voiceprompt.models import supports_language
    assert supports_language(model, "ru") is False


def test_cancelled_download_never_contacts_hub_or_publishes(tmp_path):
    destination = tmp_path / "selected-model"
    with pytest.raises(ModelError, match="model_download_cancelled"):
        download_model("Systran/faster-whisper-small", "a" * 40, destination,
                       cancelled=lambda: True)
    assert not destination.exists()


def test_download_requires_an_immutable_revision_before_network(tmp_path):
    with pytest.raises(ModelError, match="model_revision_invalid"):
        download_model("Systran/faster-whisper-small", "main", tmp_path / "model")


def test_bounded_context_uses_one_combined_token_budget():
    from tokenizers import Tokenizer, models, pre_tokenizers
    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0, "context": 1, "term": 2}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    backend = object.__new__(WhisperBackend)
    backend.tokenizer = tokenizer
    text, glossary = backend._bounded_parts("context " * 200, "term " * 80, 128)
    assert text == " ".join(["context"] * 48)
    assert glossary == " ".join(["term"] * 80)


def test_spawned_worker_reports_missing_local_assets_and_reaps(tmp_path):
    async def scenario():
        worker = Worker()
        worker.start({
            "owner_generation": "owner-1", "session_id": "session-1",
            "model_path": str(tmp_path / "missing"), "device": "cpu", "compute_type": "int8",
        })
        try:
            deadline = time.monotonic() + 5
            messages = []
            while time.monotonic() < deadline and not messages:
                messages = worker.poll()
                if not messages:
                    await asyncio.sleep(0.02)
            assert len(messages) == 1
            assert messages[0]["type"] == "load_error"
            assert messages[0]["owner_generation"] == "owner-1"
            assert messages[0]["session_id"] == "session-1"
            assert messages[0]["error_code"] == "model_missing"
        finally:
            await worker.terminate_and_reap(0.2)

    asyncio.run(scenario())
