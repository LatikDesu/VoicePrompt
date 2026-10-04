"""Installed helper entry point. Long-lived commands are added with the supervisor."""

import argparse
import asyncio
import importlib.metadata
import json
import os
import sys
import uuid
from pathlib import Path

MODEL_PROFILES = ("cpu-small", "cuda-turbo", "custom")
_PROFILE_DEVICE = {"cpu-small": "cpu", "cuda-turbo": "cuda"}


def default_model_path(profile: str) -> Path:
    """Standard XDG cache location for a prepared pinned profile."""
    cache = os.environ.get("XDG_CACHE_HOME")
    if not cache:
        home = os.environ.get("HOME") or str(Path.home())
        cache = str(Path(home) / ".cache")
    return Path(cache) / "voiceprompt" / "models" / profile


def _emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")), flush=True)

async def _supervise(runtime_dir: Path, owner: str) -> None:
    from .engine import Engine
    from .supervisor import Supervisor
    supervisor = Supervisor(runtime_dir, owner)
    engine = Engine(supervisor.emit, owner)
    await engine.prepare_runtime()
    supervisor.engine = engine
    await supervisor.run()


def _select_model(args, parser) -> tuple[str, Path]:
    expected = _PROFILE_DEVICE.get(args.model_profile)
    if expected is not None and args.device != expected:
        parser.error("device_profile_mismatch")
    if args.model_profile == "custom":
        if not args.model_path:
            parser.error("model_path_required")
        return args.model_profile, Path(args.model_path).expanduser()
    if args.model_path:
        return args.model_profile, Path(args.model_path).expanduser()
    return args.model_profile, default_model_path(args.model_profile)


def _setup_progress(downloaded: int, total: int | None) -> None:
    size = "?" if total is None else str(total)
    print(f"model_download_progress {downloaded}/{size}", file=sys.stderr, flush=True)


def _prepare_model(profile: str, path: Path, progress):
    """Prepare the pinned profile and return its validated ModelRef, or a stable code."""
    from . import models
    try:
        return models.prepare_model(profile, path, progress)
    except models.ModelError as error:
        return error.code
    except OSError:
        return "model_install_failed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="voiceprompt-helper")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("version", help="Print installed package and protocol versions")
    commands.add_parser("identity", help="Create a fresh owner generation without side effects")
    diagnostics = commands.add_parser("diagnostics", help="Inspect the local CPU runtime without capturing audio")
    diagnostics.add_argument("--json", action="store_true", help="Return machine-readable JSON")
    diagnostics.add_argument("--device", choices=["cpu", "cuda"], default="cpu", help="Explicitly probe selected STT device")
    supervise = commands.add_parser("supervise", help="Run the owned private helper command server")
    supervise.add_argument("--owner", required=True)
    supervise.add_argument("--runtime-dir", type=Path)
    control = commands.add_parser("command", help="Send one bounded JSON command to the owned helper")
    control.add_argument("--runtime-dir", type=Path)
    control.add_argument("--json", required=True)
    benchmark = commands.add_parser("benchmark", help="Compare real file-replayed CPU streaming/final to offline")
    benchmark.add_argument("--corpus", type=Path, required=True)
    benchmark.add_argument("--profile", choices=["cpu-small"], required=True)
    benchmark.add_argument("--model", type=Path, required=True)
    benchmark.add_argument("--output", type=Path, required=True)
    for name, help_text in (
        ("setup", "Prepare the selected model and smoke-load it in a child process"),
        ("setup-status", "Report readiness for the selected profile without side effects"),
    ):
        setup = commands.add_parser(name, help=help_text)
        setup.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
        setup.add_argument("--model-profile", choices=list(MODEL_PROFILES), default="cpu-small")
        setup.add_argument("--model-path", type=Path)
        setup.add_argument("--json", action="store_true", help="Return machine-readable JSON")
    args = parser.parse_args(argv)
    if args.command == "identity":
        _emit({"owner_generation": str(uuid.uuid4())})
        return 0
    try:
        version = importlib.metadata.version("voiceprompt-helper")
    except importlib.metadata.PackageNotFoundError:
        print("voiceprompt-helper is not installed; install the project wheel in an isolated environment", file=sys.stderr)
        return 2
    if args.command == "version":
        _emit({"helper_version": version, "protocol_min": 1, "protocol_max": 1, "config_schema": 1})
        return 0
    if args.command == "benchmark":
        from .benchmark import BenchmarkError, run
        from .models import ModelError
        try:
            report = run(args.corpus, args.model, args.profile, args.output)
        except (BenchmarkError, ModelError) as error:
            print(str(error), file=sys.stderr)
            return 2
        _emit({"report": str(args.output), "entries": len(report["entries"]),
               "profile": args.profile})
        return 0
    if args.command in {"supervise", "command"}:
        from .ownership import OwnershipError
        from .protocol import ProtocolError
        from .supervisor import Supervisor, send_command
        runtime_dir = args.runtime_dir
        if runtime_dir is None:
            runtime_root = os.environ.get("XDG_RUNTIME_DIR")
            if not runtime_root:
                parser.error("XDG_RUNTIME_DIR is unset; provide --runtime-dir")
            runtime_dir = Path(runtime_root) / "voiceprompt"
        try:
            if args.command == "supervise":
                asyncio.run(_supervise(runtime_dir, args.owner))
                return 0
            acknowledgement = send_command(runtime_dir, json.loads(args.json))
            _emit(acknowledgement)
            return 0 if acknowledgement.get("accepted") else 1
        except (OwnershipError, ProtocolError, OSError, ValueError) as error:
            code = getattr(error, "code", str(error) if isinstance(error, OwnershipError) else "command_failed")
            if args.command == "command":
                _emit({"protocol_version": 1, "accepted": False, "error_code": code})
            else:
                print(code, file=sys.stderr)
            return 2
    if args.command in {"setup", "setup-status"}:
        from . import diagnostics, models
        profile, model_path = _select_model(args, parser)
        progress = _setup_progress if args.command == "setup" else None
        report = diagnostics.inspect_cpu()
        if args.device == "cuda":
            report["cuda"] = diagnostics.inspect_cuda()
            report["optional"]["cuda"] = ("available" if report["cuda"]["runtime_available"]
                                          else "unavailable")
        captured = None
        if args.command == "setup":
            prepared = _prepare_model(profile, model_path, progress)
            if isinstance(prepared, models.ModelRef):
                # Reuse is only possible with the proof recorded while the model was read;
                # a reference without one (fresh download/publish) is re-read by readiness.
                captured = models.capture_validated(prepared)
                report["model_load"] = models.smoke_load(model_path, args.device)
            else:
                report["model_load"] = {"loaded": False, "error_code": prepared}
        diagnostics.readiness(report, args.device, profile, model_path, validated=captured)
        if args.command == "setup" and not report["model_load"]["loaded"]:
            report["recording_ready"] = False
            code = report["model_load"]["error_code"] or "model_load_failed"
            if code not in report["error_codes"]:
                report["error_codes"].append(code)
        report["helper_version"] = version
        report["protocol_min"] = 1
        report["protocol_max"] = 1
        report["config_schema"] = 1
        if args.json:
            _emit(report)
        else:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        if args.command == "setup":
            return 0 if (report["model_load"]["loaded"] and report["model"]["available"]
                         and report["cpu"]["runtime_available"]
                         and report["vad"]["runtime_available"]) else 1
        return 0 if report["recording_ready"] else 1
    from .diagnostics import inspect_cpu
    report = inspect_cpu()
    report["selected_device"] = args.device
    if args.device == "cuda":
        from .diagnostics import inspect_cuda
        report["cuda"] = inspect_cuda()
        report["optional"]["cuda"] = "available" if report["cuda"]["runtime_available"] else "unavailable"
    report["helper_version"] = version
    report["protocol_min"] = 1
    report["protocol_max"] = 1
    report["config_schema"] = 1
    if args.json:
        _emit(report)
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    available = report["cpu"]["runtime_available"] and report["vad"]["runtime_available"]
    if args.device == "cuda":
        available = available and report["cuda"]["runtime_available"]
    return 0 if available else 1


if __name__ == "__main__":
    raise SystemExit(main())
