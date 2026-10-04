"""GPU child-library resolution without system CUDA or CPU startup dependency."""

import ctypes
import importlib.metadata
import json
import os
from contextlib import contextmanager
from pathlib import Path

_PACKAGES = (
    ("nvidia-cublas-cu12", "nvidia/cublas/lib"),
    ("nvidia-cudnn-cu12", "nvidia/cudnn/lib"),
    ("nvidia-cuda-nvrtc-cu12", "nvidia/cuda_nvrtc/lib"),
)


def library_directories() -> list[str]:
    directories = []
    for name, relative in _PACKAGES:
        try:
            path = Path(importlib.metadata.distribution(name).locate_file(relative))
        except importlib.metadata.PackageNotFoundError:
            continue
        if path.is_dir():
            directories.append(str(path))
    return directories


def child_environment() -> dict[str, str]:
    env = dict(os.environ)
    directories = library_directories()
    if directories:
        previous = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = os.pathsep.join(directories + ([previous] if previous else []))
    return env


@contextmanager
def spawn_environment(device: str):
    """multiprocessing.spawn inherits env; restore parent before returning to its loop.

    Worker.start runs synchronously in the single supervisor event-loop thread.
    Do not hold this context across an await or use it for concurrent thread spawns.
    """
    if device != "cuda":
        yield
        return
    previous = os.environ.get("LD_LIBRARY_PATH")
    prepared = child_environment().get("LD_LIBRARY_PATH")
    if prepared is not None:
        os.environ["LD_LIBRARY_PATH"] = prepared
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("LD_LIBRARY_PATH", None)
        else:
            os.environ["LD_LIBRARY_PATH"] = previous


def probe() -> dict:
    """Run only in an explicitly requested fresh GPU diagnostic subprocess."""
    result = {"runtime_available": False, "libraries": {}, "missing_libraries": [],
              "compute_types": [], "gpu_count": 0, "packages": {}, "model_loaded": False}
    for name, _ in _PACKAGES:
        try:
            result["packages"][name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result["packages"][name] = None
    for name in ("libcublasLt.so.12", "libcublas.so.12", "libcudnn.so.9", "libnvrtc.so.12"):
        try:
            ctypes.CDLL(name)
            result["libraries"][name] = "loadable"
        except OSError:
            result["libraries"][name] = "missing"
            result["missing_libraries"].append(name)
    if result["missing_libraries"]:
        result["error_code"] = "cuda_libraries_missing"
        return result
    try:
        import ctranslate2
        result["gpu_count"] = ctranslate2.get_cuda_device_count()
        if result["gpu_count"] == 0:
            result["error_code"] = "cuda_device_unavailable"
            return result
        result["compute_types"] = sorted(ctranslate2.get_supported_compute_types("cuda"))
        result["runtime_available"] = "int8_float16" in result["compute_types"]
        if not result["runtime_available"]:
            result["error_code"] = "compute_type_unsupported"
    except (ImportError, OSError, RuntimeError):
        result["error_code"] = "cuda_runtime_unavailable"
    return result


if __name__ == "__main__":
    report = probe()
    print(json.dumps(report, separators=(",", ":")), flush=True)
    raise SystemExit(0 if report["runtime_available"] else 1)
