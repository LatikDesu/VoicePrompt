"""Installer wheel selection: exact recorded artifact, never glob order or newest mtime.

Each scenario runs the real ``scripts/install.sh`` with a stub ``uv`` on ``PATH`` and a stub
helper executable, so no build, wheel download, venv or model access happens. The stub logs
every ``uv`` invocation and records which wheel the installer chose.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = REPO_ROOT / "scripts" / "install.sh"
WHEEL_RECORD = "helper-wheel.txt"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")

_STUB_UV = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$UV_LOG"
case "${1:-}" in
    venv)
        target="${@: -1}"
        mkdir -p "$target/bin"
        cat > "$target/bin/python" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
        cat > "$target/bin/voiceprompt-helper" <<'STUB'
#!/usr/bin/env bash
if [ "${1:-}" = version ]; then
    printf '%s\n' 0.1.0
fi
exit 0
STUB
        chmod +x "$target/bin/python" "$target/bin/voiceprompt-helper"
        ;;
    build|export)
        project=""; out=""; output=""
        shift
        while [ "$#" -gt 0 ]; do
            case "$1" in
                --project) project="$2"; shift 2 ;;
                --out-dir) out="$2"; shift 2 ;;
                --output-file) output="$2"; shift 2 ;;
                *) shift ;;
            esac
        done
        if [ -n "$out" ]; then
            version="$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*$/\1/p' "$project/pyproject.toml")"
            mkdir -p "$out"
            : > "$out/voiceprompt_helper-$version-py3-none-any.whl"
        fi
        if [ -n "$output" ]; then
            mkdir -p "$(dirname "$output")"
            : > "$output"
        fi
        ;;
esac
exit 0
"""


def _checkout(tmp_path: Path, *, version: str, wheels: tuple[str, ...]) -> Path:
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(INSTALLER, root / "scripts" / "install.sh")
    (root / "plugin" / "voice-prompt").mkdir(parents=True)
    helper = root / "helper"
    (helper / "src" / "voiceprompt").mkdir(parents=True)
    (helper / "src" / "voiceprompt" / "models.py").write_text("VALUE = 1\n", encoding="utf-8")
    (helper / "pyproject.toml").write_text(
        f'[project]\nname = "voiceprompt-helper"\nversion = "{version}"\n', encoding="utf-8")
    (helper / "uv.lock").write_text("", encoding="utf-8")
    dist = root / "dist" / "helper"
    dist.mkdir(parents=True)
    (dist / "cpu-constraints.txt").write_text("", encoding="utf-8")
    for candidate in wheels:
        (dist / f"voiceprompt_helper-{candidate}-py3-none-any.whl").write_bytes(b"wheel")
    return root


def _environment(tmp_path: Path, log: Path) -> dict:
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir(exist_ok=True)
    stub = stub_bin / "uv"
    stub.write_text(_STUB_UV, encoding="utf-8")
    stub.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{stub_bin}{os.pathsep}{environment.get('PATH', '')}"
    environment["UV_LOG"] = str(log)
    environment["XDG_DATA_HOME"] = str(tmp_path / "data")
    environment["XDG_CACHE_HOME"] = str(tmp_path / "cache")
    return environment


def _run(tmp_path: Path, script: Path) -> subprocess.CompletedProcess:
    log = tmp_path / "uv.log"
    log.unlink(missing_ok=True)
    return subprocess.run(
        ["bash", str(script), "--no-plugin"], cwd=str(tmp_path),
        env=_environment(tmp_path, log),
        capture_output=True, text=True, timeout=120, check=False,
    )


def _uv_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "uv.log"
    if not log.is_file():
        return []
    return log.read_text(encoding="utf-8").splitlines()


def _installed_wheel(tmp_path: Path) -> str:
    lines = [line for line in _uv_calls(tmp_path) if line.startswith("pip install")]
    assert len(lines) == 1, lines
    return lines[0]


def _retained_wheels(tmp_path: Path) -> list[str]:
    dist = tmp_path / "data" / "voiceprompt" / "dist" / "helper"
    return sorted(item.name for item in dist.glob("voiceprompt_helper-*.whl"))


def _install_state(install_root: Path) -> dict:
    """Everything a refused recovery run must leave exactly as it was."""
    dist = install_root / "dist" / "helper"
    record = dist / WHEEL_RECORD
    return {
        "wheels": {item.name: item.read_bytes()
                   for item in sorted(dist.glob("voiceprompt_helper-*.whl"))},
        "record": record.read_text(encoding="utf-8") if record.is_file() else None,
        "constraints": (dist / "cpu-constraints.txt").read_bytes(),
        "helper": (install_root / "helper" / "bin" / "voiceprompt-helper").read_bytes(),
    }


def test_checkout_install_selects_the_wheel_declared_by_source_metadata(tmp_path):
    root = _checkout(tmp_path, version="0.9.0", wheels=("0.9.0", "0.10.0"))

    completed = _run(tmp_path, root / "scripts" / "install.sh")

    assert completed.returncode == 0, completed.stderr
    installed = _installed_wheel(tmp_path)
    assert "voiceprompt_helper-0.9.0-py3-none-any.whl" in installed
    assert "0.10.0" not in installed
    assert _retained_wheels(tmp_path) == ["voiceprompt_helper-0.9.0-py3-none-any.whl"]
    assert (root / "dist" / "helper" / "voiceprompt_helper-0.10.0-py3-none-any.whl").exists()
    record = tmp_path / "data" / "voiceprompt" / "dist" / "helper" / WHEEL_RECORD
    assert record.read_text(encoding="utf-8").strip() == "voiceprompt_helper-0.9.0-py3-none-any.whl"


def test_recovery_selects_the_recorded_wheel_and_keeps_superseded_wheels(tmp_path):
    root = _checkout(tmp_path, version="0.9.0", wheels=("0.9.0", "0.10.0"))
    first = _run(tmp_path, root / "scripts" / "install.sh")
    assert first.returncode == 0, first.stderr

    install_root = tmp_path / "data" / "voiceprompt"
    dist = install_root / "dist" / "helper"
    older = dist / "voiceprompt_helper-0.8.0-py3-none-any.whl"
    newer = dist / "voiceprompt_helper-0.10.0-py3-none-any.whl"
    older.write_bytes(b"retained-older")
    newer.write_bytes(b"retained-newer")
    future = time.time() + 86400
    os.utime(newer, (future, future))
    retained_before = sorted(item.name for item in dist.glob("voiceprompt_helper-*.whl"))
    retained_installer = install_root / "scripts" / "install.sh"
    assert retained_installer.is_file()

    completed = _run(tmp_path, retained_installer)

    assert completed.returncode == 0, completed.stderr
    installed = _installed_wheel(tmp_path)
    assert "voiceprompt_helper-0.9.0-py3-none-any.whl" in installed
    assert "0.8.0" not in installed and "0.10.0" not in installed
    assert (dist / WHEEL_RECORD).read_text(encoding="utf-8").strip() == (
        "voiceprompt_helper-0.9.0-py3-none-any.whl")
    assert older.read_bytes() == b"retained-older"
    assert newer.read_bytes() == b"retained-newer"
    assert sorted(item.name for item in dist.glob("voiceprompt_helper-*.whl")) == retained_before


def test_recovery_refuses_a_missing_recorded_wheel_instead_of_downgrading(tmp_path):
    root = _checkout(tmp_path, version="0.9.0", wheels=("0.9.0",))
    assert _run(tmp_path, root / "scripts" / "install.sh").returncode == 0

    install_root = tmp_path / "data" / "voiceprompt"
    dist = install_root / "dist" / "helper"
    (dist / "voiceprompt_helper-0.9.0-py3-none-any.whl").unlink()
    fallback = dist / "voiceprompt_helper-0.10.0-py3-none-any.whl"
    fallback.write_bytes(b"superseded")
    before = _install_state(install_root)

    completed = _run(tmp_path, install_root / "scripts" / "install.sh")

    assert completed.returncode != 0
    assert "refusing to substitute another version" in completed.stderr
    assert fallback.read_bytes() == b"superseded"
    assert _install_state(install_root) == before
    assert _uv_calls(tmp_path) == []


def test_recovery_refuses_to_guess_between_unrecorded_wheels(tmp_path):
    root = _checkout(tmp_path, version="0.9.0", wheels=("0.9.0",))
    assert _run(tmp_path, root / "scripts" / "install.sh").returncode == 0

    install_root = tmp_path / "data" / "voiceprompt"
    dist = install_root / "dist" / "helper"
    (dist / WHEEL_RECORD).unlink()
    extra = dist / "voiceprompt_helper-0.10.0-py3-none-any.whl"
    extra.write_bytes(b"superseded")
    before = _install_state(install_root)

    completed = _run(tmp_path, install_root / "scripts" / "install.sh")

    assert completed.returncode != 0
    assert "refusing to guess" in completed.stderr
    assert extra.read_bytes() == b"superseded"
    assert (dist / "voiceprompt_helper-0.9.0-py3-none-any.whl").is_file()
    assert _install_state(install_root) == before
    assert _uv_calls(tmp_path) == []
