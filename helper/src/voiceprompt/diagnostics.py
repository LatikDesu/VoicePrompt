"""Read-only CPU diagnostics and opt-in isolated CUDA probes; never capture/download."""

import importlib
import importlib.metadata
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

_PACKAGES = (
    "faster-whisper",
    "ctranslate2",
    "onnxruntime",
    "numpy",
    "av",
    "huggingface-hub",
)


def _package_versions() -> dict[str, str | None]:
    versions = {}
    for name in _PACKAGES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _cpu_features() -> list[str]:
    try:
        with Path("/proc/cpuinfo").open() as stream:
            for line in stream:
                if line.startswith("flags"):
                    flags = set(line.partition(":")[2].split())
                    return sorted(flags.intersection({"sse4_1", "sse4_2", "avx", "avx2", "avx512f"}))
    except OSError:
        return []
    return []


def _pipewire() -> dict:
    executable = shutil.which("pw-record")
    server_tool = shutil.which("pw-cli")
    result = {"executable": executable, "version": None, "server_reachable": None}
    if executable:
        try:
            version = subprocess.run(
                [executable, "--version"], capture_output=True, text=True,
                timeout=3, check=False,
            )
            if version.returncode == 0:
                result["version"] = version.stdout.strip()[:512]
        except (OSError, subprocess.TimeoutExpired):
            result["error_code"] = "pipewire_version_unavailable"
    if server_tool:
        try:
            status = subprocess.run(
                [server_tool, "info", "0"], capture_output=True,
                timeout=3, check=False,
            )
            result["server_reachable"] = status.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            result["server_reachable"] = False
    else:
        fallback_tool = shutil.which("wpctl")
        if fallback_tool:
            try:
                status = subprocess.run(
                    [fallback_tool, "status"], capture_output=True,
                    timeout=3, check=False,
                )
                result["server_reachable"] = status.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                result["server_reachable"] = False
    return result


def _pipewire_error_code(pipewire: dict) -> str | None:
    """Return a stable readiness code; unknown reachability is never treated as ready."""
    if not pipewire.get("executable"):
        return "pipewire_unavailable"
    reachable = pipewire.get("server_reachable")
    if reachable is True:
        return None
    return "pipewire_unreachable" if reachable is False else "pipewire_unverified"


def model_state(path, profile: str, validated=None) -> dict:
    """Validate a prepared model directory on disk without loading it.

    ``validated`` may carry a ModelRef whose validation proof was recorded by the reads
    that vetted this profile; it is reused only while the directory still matches that
    proof (identity, size, mtime, ctime and permissions). Anything else — including a
    reference without a proof — falls back to reading the model from disk.
    """
    from . import models
    if validated is not None:
        try:
            models.check_profile(validated.ref, profile)
        except models.ModelError:
            pass
        else:
            if validated.matches(path):
                return {"available": True, "error_code": None}
    try:
        models.validate_profile(path, profile)
    except models.ModelError as error:
        return {"available": False, "error_code": error.code}
    return {"available": True, "error_code": None}


def readiness(report: dict, device: str, model_profile: str, model_path, validated=None) -> dict:
    """Extend an inspect_cpu report with the selected profile's readiness fields."""
    report["selected_device"] = device
    report["model_profile"] = model_profile
    report["model_path"] = str(Path(model_path).expanduser())
    report["model"] = model_state(model_path, model_profile, validated)
    codes: list[str] = []

    def add(code: str | None) -> None:
        if code and code not in codes:
            codes.append(code)

    if not report["cpu"].get("runtime_available"):
        add(report["cpu"].get("error_code") or "cpu_runtime_unavailable")
    if not report["vad"].get("runtime_available"):
        add(report["vad"].get("error_code") or "vad_runtime_unavailable")
    add(_pipewire_error_code(report.get("pipewire") or {}))
    if not report["model"]["available"]:
        add(report["model"]["error_code"] or "model_missing")
    if device == "cuda":
        cuda = report.get("cuda") or {}
        if not cuda.get("runtime_available"):
            add(cuda.get("error_code") or "cuda_runtime_unavailable")
    report["error_codes"] = codes
    report["recording_ready"] = not codes
    return report


def inspect_cpu() -> dict:
    """Inspect installed CPU libraries without advertising a loaded STT model."""
    libc, libc_version = platform.libc_ver()
    result = {
        "python": platform.python_version(),
        "platform": {"system": platform.system(), "architecture": platform.machine(),
                     "libc": libc, "libc_version": libc_version},
        "packages": _package_versions(),
        "cpu": {"runtime_available": False, "compute_types": [],
                "instruction_features": _cpu_features()},
        "vad": {"runtime_available": False, "providers": []},
        "pipewire": _pipewire(),
        "model": {"loaded": False},
        "optional": {"cuda": "not_probed", "evdev": "not_probed"},
    }
    try:
        ct2 = importlib.import_module("ctranslate2")
        importlib.import_module("faster_whisper")
        compute_types = sorted(ct2.get_supported_compute_types("cpu"))
        result["cpu"]["compute_types"] = compute_types
        result["cpu"]["runtime_available"] = "int8" in compute_types
    except (ImportError, OSError, RuntimeError):
        result["cpu"]["error_code"] = "cpu_runtime_unavailable"
    try:
        ort = importlib.import_module("onnxruntime")
        providers = ort.get_available_providers()
        result["vad"]["providers"] = providers
        if "CPUExecutionProvider" in providers:
            from .vad import SileroScorer
            import numpy as np

            SileroScorer().score(np.zeros(512, dtype=np.float32))
            result["vad"]["runtime_available"] = True
    except Exception:
        result["vad"]["error_code"] = "vad_runtime_unavailable"
    return result


def inspect_cuda() -> dict:
    """Probe GPU libraries in a fresh child, not the lightweight supervisor."""
    from .cuda_runtime import child_environment
    failed = {"runtime_available": False, "error_code": "cuda_diagnostics_invalid"}
    try:
        result = subprocess.run([sys.executable, "-m", "voiceprompt.cuda_runtime"],
                                env=child_environment(), capture_output=True, text=True,
                                timeout=15, check=False)
        if len(result.stdout) > 262144:
            return failed
        report = json.loads(result.stdout)
        if not isinstance(report, dict) or type(report.get("runtime_available")) is not bool:
            return failed
        return report
    except subprocess.TimeoutExpired:
        return {"runtime_available": False, "error_code": "cuda_diagnostics_timeout"}
    except (OSError, ValueError):
        return failed
