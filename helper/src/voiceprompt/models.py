"""Explicit, revision-pinned model setup and offline CT2 model validation."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class ModelError(ValueError):
    """Stable, user-actionable model/setup failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ModelRef:
    repo: str | None
    revision: str | None
    local_path: Path
    checksum_manifest: dict[str, str]
    # Directory signature recorded while this reference was validated; None for references
    # reconstructed from stored configuration (no reuse claim attached).
    fingerprint: tuple | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "local_path", Path(self.local_path).resolve())
        object.__setattr__(self, "checksum_manifest", dict(self.checksum_manifest))


_ASSETS = frozenset({
    "model.bin", "config.json", "tokenizer.json", "vocabulary.json", "vocabulary.txt",
    "preprocessor_config.json", "tokenizer_config.json",
    "special_tokens_map.json", "added_tokens.json",
})
_REQUIRED = frozenset({"model.bin", "config.json"})
_REVISION_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_CHUNK_SIZE = 1024 * 1024


def _check_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise ModelError("model_download_cancelled")


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(_CHUNK_SIZE):
                digest.update(chunk)
    except OSError:
        raise ModelError("model_unreadable") from None
    return digest.hexdigest()


def _stat_signature(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    """Identity, permission and time signature of one path.

    Includes ``st_ctime_ns`` and ``st_mode`` so that rewriting a file with a restored
    ``mtime`` or changing permissions still invalidates anything captured earlier.
    """
    ctime_ns = getattr(info, "st_ctime_ns", None)
    if ctime_ns is None:
        ctime_ns = int(info.st_ctime * 1_000_000_000)
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, ctime_ns)


def _directory_signature(path: Path) -> tuple:
    """Stat-only signature including link identity and followed target state."""
    root = _stat_signature(path.stat())
    entries = []
    for entry in sorted(path.iterdir()):
        identity = entry.lstat()
        target = _stat_signature(entry.stat()) if stat.S_ISLNK(identity.st_mode) else None
        entries.append((entry.name, _stat_signature(identity), target))
    return (root, tuple(entries))


_PROVENANCE_FILE = ".voiceprompt-model.json"


def _validate_directory(
    path: Path,
    repo: str | None,
    revision: str | None,
    *,
    digests: dict[str, str] | None = None,
) -> ModelRef:
    """Validate asset structure and identity, recording a proof of the validated bytes.

    The directory is stat'ed before and after the reads; any difference (content, size,
    mtime, ctime or permissions) fails validation instead of recording an ambiguous state.
    The returned reference carries the verified signature so a later change detector can
    consult it, and callers that only need a change detector can use ``model_fingerprint``.

    ``digests`` may carry checksums captured for these same bytes earlier in the same
    operation (for example by ``download_model`` while streaming the files it wrote).
    Structure, required assets and declared provenance are still checked; only the
    redundant re-read/re-hash of the same bytes is skipped. Callers at a fresh trust
    boundary must omit ``digests`` so the bytes are hashed as read from disk.
    """
    if not path.is_dir():
        raise ModelError("model_missing")
    for name in _REQUIRED:
        target = path / name
        if not target.is_file() or target.stat().st_size == 0:
            raise ModelError("model_missing")

    tokenizer_paths = [path / "tokenizer.json", path / "vocabulary.json"]
    present_tokenizers = [target for target in tokenizer_paths if target.is_file()]
    if not (path / "tokenizer.json").is_file():
        raise ModelError("tokenizer_missing")

    # Proof that the recorded signature describes the bytes read below: the directory is
    # re-stat'ed after the reads and any difference fails instead of blessing one state.
    try:
        signature_before = _directory_signature(path)
    except OSError:
        raise ModelError("model_unreadable") from None

    try:
        config = json.loads((path / "config.json").read_text(encoding="utf-8"))
        if not isinstance(config, dict) or config.get("model_type") not in {None, "Whisper"}:
            raise ModelError("model_format_invalid")
        for tokenizer in present_tokenizers:
            parsed = json.loads(tokenizer.read_text(encoding="utf-8"))
            if not isinstance(parsed, (dict, list)):
                raise ModelError("model_corrupt")
    except ModelError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ModelError("model_corrupt") from None

    provenance_path = path / _PROVENANCE_FILE
    declared = None
    if provenance_path.is_file():
        try:
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ModelError("model_manifest_invalid") from None
        if (not isinstance(provenance, dict) or provenance.get("schema") != 1
                or not isinstance(provenance.get("repo"), str)
                or not _REPO_RE.fullmatch(provenance["repo"])
                or not isinstance(provenance.get("revision"), str)
                or not _REVISION_RE.fullmatch(provenance["revision"])
                or not isinstance(provenance.get("checksums"), dict)):
            raise ModelError("model_manifest_invalid")
        repo = provenance["repo"]
        revision = provenance["revision"].lower()
        declared = provenance["checksums"]
        if any(name not in _ASSETS or not isinstance(value, str)
               or not _HEX64_RE.fullmatch(value) for name, value in declared.items()):
            raise ModelError("model_manifest_invalid")

    if digests is None:
        manifest: dict[str, str] = {}
        for name in sorted(_ASSETS):
            target = path / name
            if target.is_file():
                manifest[name] = _digest(target)
    else:
        manifest = dict(digests)
    if declared is not None and declared != manifest:
        raise ModelError("model_checksum_mismatch")
    try:
        signature_after = _directory_signature(path)
    except OSError:
        raise ModelError("model_unreadable") from None
    if signature_after != signature_before:
        # The tree changed while it was read: the digests describe no single on-disk state.
        raise ModelError("model_unreadable")
    return ModelRef(repo, revision, path, manifest, signature_after)


def validate_local(path: str | os.PathLike[str]) -> ModelRef:
    """Validate a prepared CT2 Whisper directory without making network calls."""
    return _validate_directory(Path(path).expanduser(), None, None)


def check_profile(ref: ModelRef, profile: str) -> None:
    """Require exactly the (repo, revision) pinned for the profile.

    A model of the same repository at any other commit is not the pinned profile and is
    rejected; the ``custom`` profile carries no pinned identity and is left unchecked.
    """
    from .model_download import PROFILES
    expected = PROFILES.get(profile)
    if expected is not None and (ref.repo != expected[0] or ref.revision != expected[1]):
        raise ModelError("model_profile_mismatch")


def validate_profile(path: str | os.PathLike[str], profile: str) -> ModelRef:
    ref = validate_local(path)
    check_profile(ref, profile)
    return ref


def model_fingerprint(path: Path) -> tuple | None:
    """Stat-only signature of a directory, or ``None`` when the path does not exist.

    Covers the directory and every entry with inode, mode, size, mtime and ctime, so a
    rewrite with a restored ``mtime`` or a permission change is still a difference. This
    is a change detector only: it never substitutes for reading the validated bytes.
    """
    if path.is_symlink():
        raise ModelError("model_destination_exists")
    if not path.exists():
        return None
    try:
        if not path.is_dir():
            return (_stat_signature(path.stat()), ())
        return _directory_signature(path)
    except OSError:
        raise ModelError("model_unreadable") from None


@dataclass(frozen=True, slots=True)
class ValidatedModel:
    """A ModelRef plus the proof recorded while its bytes were validated.

    ``proof`` is the signature taken inside ``_validate_directory`` (before and after the
    reads, required to be equal), never a later stat of the reference's directory. A
    reference without such a proof can never be reused, and a proof that no longer matches
    the directory falls back to reading the model again.
    """

    ref: ModelRef
    proof: tuple | None

    def matches(self, path: str | os.PathLike[str]) -> bool:
        if self.proof is None:
            return False
        try:
            candidate = Path(path).expanduser().resolve()
        except OSError:
            return False
        if candidate != self.ref.local_path:
            return False
        try:
            return model_fingerprint(candidate) == self.proof
        except ModelError:
            return False


def capture_validated(ref: ModelRef) -> ValidatedModel:
    """Attach the validation proof recorded on ``ref``, if it carries one.

    References created without validation reads (reconstructed configurations, artifacts
    published by a rename) carry no proof, so callers must validate them from disk.
    """
    return ValidatedModel(ref, ref.fingerprint)


def _owned_model(path: Path, profile: str) -> bool:
    """True when the directory is a pure artifact of this profile's pinned repository.

    Authorization to replace a directory deliberately does not compare revisions: an
    owned artifact recorded at a superseded revision is refreshed to the current pinned
    revision, while the exact pinned revision is still required for reuse (check_profile).
    """
    from .model_download import PROFILES
    if path.is_symlink() or not path.is_dir():
        return False
    try:
        if any(entry.name not in _ASSETS | {_PROVENANCE_FILE} or entry.is_symlink() for entry in path.iterdir()):
            return False
        marker = path / _PROVENANCE_FILE
        if marker.stat().st_size > 65536:
            return False
        metadata = json.loads(marker.read_text(encoding="utf-8"))
        return (metadata.get("schema") == 1 and metadata.get("repo") == PROFILES[profile][0]
                and isinstance(metadata.get("revision"), str) and bool(_REVISION_RE.fullmatch(metadata["revision"])))
    except (OSError, ValueError, AttributeError):
        return False


def publish_model(staged: Path, target: Path, profile: str, fingerprint: tuple | None) -> ModelRef:
    ref = validate_profile(staged, profile)
    if model_fingerprint(target) != fingerprint:
        raise ModelError("model_destination_changed")
    backup = None
    if fingerprint is not None:
        if not _owned_model(target, profile):
            raise ModelError("model_destination_exists")
        backup = target.parent / f".{target.name}.replaced-{uuid.uuid4().hex}"
    try:
        if backup is not None:
            os.rename(target, backup)
        os.rename(staged, target)
    except BaseException:
        if backup is not None and backup.exists() and not target.exists():
            os.rename(backup, target)
        raise
    if backup is not None:
        shutil.rmtree(backup)
    return ModelRef(ref.repo, ref.revision, target, ref.checksum_manifest)


def prepare_model(profile: str, path: Path, progress=None, *, staging: Path | None = None,
                  publish: bool = True) -> ModelRef:
    """Reuse the exact pinned model; repair only owned artifacts and preserve unrelated data.

    Only the exact pinned (repo, revision) is reused. An artifact owned by the pinned
    repository but recorded at a superseded revision is replaced, while directories that
    are not exclusively this profile's own artifact are never touched.
    """
    path = Path(path).expanduser()
    fingerprint = model_fingerprint(path)
    try:
        return validate_profile(path, profile)
    except ModelError:
        if profile == "custom":
            raise
        # A pinned profile replaces only artifacts it owns itself; unrelated or foreign
        # directories are never touched. An owned artifact whose recorded revision was
        # superseded is refreshed to the current pinned revision.
        if fingerprint is not None and not _owned_model(path, profile):
            raise
    from .model_download import PROFILES
    repo, revision = PROFILES[profile]
    if fingerprint is None and publish:
        return download_model(repo, revision, path, progress=progress)
    staged = staging or (path.parent / f".{path.name}.repair-{uuid.uuid4().hex}")
    try:
        ref = download_model(repo, revision, staged, progress=progress)
        return publish_model(staged, path, profile, fingerprint) if publish else ref
    finally:
        if publish and staged.exists():
            shutil.rmtree(staged)



def supports_language(model_ref: ModelRef, language: str) -> bool | None:
    """Return tokenizer evidence for a language, or None when assets are opaque."""
    if language not in {"ru", "en"}:
        return True
    candidates = (model_ref.local_path / "tokenizer.json", model_ref.local_path / "vocabulary.json")
    tokens: set[str] = set()
    try:
        for path in candidates:
            if not path.is_file():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                vocab = data.get("model", {}).get("vocab") if isinstance(data.get("model"), dict) else None
                if isinstance(vocab, dict):
                    tokens.update(key for key in vocab if isinstance(key, str))
                vocab = data.get("vocab")
                if isinstance(vocab, dict):
                    tokens.update(key for key in vocab if isinstance(key, str))
                added = data.get("added_tokens")
                if isinstance(added, list):
                    tokens.update(item["content"] for item in added
                                  if isinstance(item, dict) and isinstance(item.get("content"), str))
            elif isinstance(data, list):
                tokens.update(item for item in data if isinstance(item, str))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ModelError("model_corrupt") from None
    if not tokens:
        return None
    marker = f"<|{language}|>"
    if marker in tokens:
        return True
    if "<|en|>" in tokens or "<|ru|>" in tokens:
        return False
    return None


def _model_metadata(repo: str, revision: str) -> tuple[list[dict], str]:
    if not _REPO_RE.fullmatch(repo) or not _REVISION_RE.fullmatch(revision):
        raise ModelError("model_revision_invalid")
    url = ("https://huggingface.co/api/models/" + urllib.parse.quote(repo, safe="/")
           + "/revision/" + urllib.parse.quote(revision.lower(), safe="") + "?blobs=true")
    request = urllib.request.Request(url, headers={"User-Agent": "voiceprompt-helper/0.1.0"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise ModelError("model_revision_invalid") from None
        raise ModelError("model_download_failed") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ModelError("model_download_failed") from None
    if len(raw) > 8 * 1024 * 1024:
        raise ModelError("model_metadata_invalid")
    try:
        info = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        raise ModelError("model_metadata_invalid") from None
    if not isinstance(info, dict) or info.get("sha") != revision.lower():
        raise ModelError("model_revision_invalid")
    siblings = info.get("siblings")
    if not isinstance(siblings, list):
        raise ModelError("model_metadata_invalid")
    return siblings, info["sha"]


def _disk_error(error: OSError) -> ModelError:
    if error.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}:
        return ModelError("model_disk_full")
    return ModelError("model_download_failed")


def download_model(
    repo: str,
    revision: str,
    destination: str | os.PathLike[str],
    progress: Callable[[int, int | None], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> ModelRef:
    """Download only CT2 model assets from an immutable Hub commit, then publish atomically.

    Progress callbacks receive cumulative ``(downloaded_bytes, total_bytes|None)``.
    Cancellation is checked before metadata access, between files and each streamed chunk.
    """
    _check_cancelled(cancelled)
    siblings, pinned_revision = _model_metadata(repo, revision)
    selected: dict[str, dict] = {}
    for entry in siblings:
        if not isinstance(entry, dict):
            raise ModelError("model_metadata_invalid")
        name = entry.get("rfilename")
        if name in _ASSETS:
            selected[name] = entry
    if not _REQUIRED.issubset(selected):
        raise ModelError("model_missing")
    if "tokenizer.json" not in selected:
        raise ModelError("tokenizer_missing")

    total: int | None = 0
    for entry in selected.values():
        size = entry.get("size")
        if type(size) is not int or size < 0:
            total = None
            break
        total += size

    target = Path(destination).expanduser().absolute()
    if target.exists():
        raise ModelError("model_destination_exists")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.voiceprompt-", dir=target.parent))
        os.chmod(staging, 0o700)
    except OSError as error:
        raise _disk_error(error) from None

    downloaded = 0
    try:
        if progress is not None:
            progress(downloaded, total)
        manifest: dict[str, str] = {}
        for name, entry in selected.items():
            _check_cancelled(cancelled)
            url = ("https://huggingface.co/" + urllib.parse.quote(repo, safe="/")
                   + "/resolve/" + urllib.parse.quote(pinned_revision.lower(), safe="") + "/"
                   + urllib.parse.quote(name, safe="") + "?download=true")
            request = urllib.request.Request(url, headers={"User-Agent": "voiceprompt-helper/0.1.0"})
            digest = hashlib.sha256()
            file_path = staging / name
            expected_hash = (entry.get("lfs") or {}).get("sha256") if isinstance(entry.get("lfs"), dict) else None
            try:
                with urllib.request.urlopen(request, timeout=10) as response, file_path.open("xb") as output:
                    os.chmod(file_path, 0o600)
                    while True:
                        _check_cancelled(cancelled)
                        chunk = response.read(_CHUNK_SIZE)
                        if not chunk:
                            break
                        output.write(chunk)
                        digest.update(chunk)
                        downloaded += len(chunk)
                        if progress is not None:
                            progress(downloaded, total)
                    output.flush()
                    os.fsync(output.fileno())
            except ModelError:
                raise
            except OSError as error:
                raise _disk_error(error) from None
            except (urllib.error.URLError, TimeoutError):
                raise ModelError("model_download_failed") from None
            actual_hash = digest.hexdigest()
            if isinstance(expected_hash, str) and actual_hash != expected_hash.lower():
                raise ModelError("model_checksum_mismatch")
            expected_size = entry.get("size")
            if type(expected_size) is int and file_path.stat().st_size != expected_size:
                raise ModelError("model_checksum_mismatch")
            manifest[name] = actual_hash

        _check_cancelled(cancelled)
        # Digests were produced while streaming exactly these bytes in this operation, so
        # validation checks structure/identity without re-reading the files it just wrote.
        # The child smoke load and the publishing parent still hash the bytes from disk.
        _validate_directory(staging, repo, pinned_revision, digests=manifest)
        provenance = json.dumps({
            "schema": 1, "repo": repo, "revision": pinned_revision.lower(),
            "checksums": manifest,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        provenance_path = staging / _PROVENANCE_FILE
        try:
            with provenance_path.open("xb") as stream:
                os.chmod(provenance_path, 0o600)
                stream.write(provenance)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as error:
            raise _disk_error(error) from None
        if target.exists():
            raise ModelError("model_destination_exists")
        try:
            os.rename(staging, target)
        except OSError as error:
            raise _disk_error(error) from None
        return ModelRef(repo, pinned_revision.lower(), target, manifest)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


_SMOKE_COMPUTE_TYPES = {"cpu": "int8", "cuda": "int8_float16"}


def smoke_load(path: str | os.PathLike[str], device: str) -> dict:
    """Load a validated local model in a fresh child process; never opens a microphone.

    Returns ``{"loaded": bool, "error_code": str|None}``. The child validates the
    directory and constructs the real faster-whisper backend, then exits so every
    model/runtime resource is released again.
    """
    if device not in _SMOKE_COMPUTE_TYPES:
        return {"loaded": False, "error_code": "device_invalid"}
    payload = json.dumps({"path": str(Path(path).expanduser().resolve()), "device": device})
    environment = None
    if device == "cuda":
        from .cuda_runtime import child_environment
        environment = child_environment()
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "voiceprompt.models", "--smoke-load"],
            input=payload, capture_output=True, text=True, timeout=300,
            check=False, env=environment,
        )
    except subprocess.TimeoutExpired:
        return {"loaded": False, "error_code": "model_load_timeout"}
    except OSError:
        return {"loaded": False, "error_code": "model_load_failed"}
    if len(completed.stdout) > 262144:
        return {"loaded": False, "error_code": "model_load_failed"}
    try:
        report = json.loads(completed.stdout)
    except (UnicodeError, json.JSONDecodeError):
        return {"loaded": False, "error_code": "model_load_failed"}
    if not isinstance(report, dict) or type(report.get("loaded")) is not bool:
        return {"loaded": False, "error_code": "model_load_failed"}
    if not report["loaded"]:
        report["error_code"] = report.get("error_code") or "model_load_failed"
    return report


def _smoke_load_child() -> int:
    from . import models as canonical
    from .backend import WhisperBackend
    try:
        payload = json.loads(sys.stdin.read(65536))
        device = payload["device"]
        if device not in canonical._SMOKE_COMPUTE_TYPES:
            raise canonical.ModelError("device_invalid")
        ref = canonical.validate_local(payload["path"])
        WhisperBackend(ref, device, canonical._SMOKE_COMPUTE_TYPES[device])
    except canonical.ModelError as error:
        report = {"loaded": False, "error_code": error.code}
    except Exception:
        report = {"loaded": False, "error_code": "model_load_failed"}
    else:
        report = {"loaded": True, "error_code": None}
    print(json.dumps(report, separators=(",", ":")), flush=True)
    return 0 if report["loaded"] else 1


if __name__ == "__main__":
    if "--smoke-load" in sys.argv:
        raise SystemExit(_smoke_load_child())
    raise SystemExit(2)
