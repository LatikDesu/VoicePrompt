"""Exercise actual helper subprocesses and private Unix command sockets."""

import json
import os
import selectors
import socket
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest


@contextmanager
def running_helper(runtime: Path, owner="g1"):
    process = subprocess.Popen(
        [sys.executable, "-m", "voiceprompt.cli", "supervise", "--owner", owner,
         "--runtime-dir", str(runtime)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        events = selector.select(timeout=5)
        selector.close()
        assert events, "supervisor must emit ready before model loading"
        line = process.stdout.readline()
        assert line, process.stderr.read().decode()
        ready = json.loads(line)
        assert ready["event"] == "ready"
        yield process, ready
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()


def command(runtime: Path, name, owner="g1", request_id="g1:r1", payload=None, session=None):
    envelope = {"protocol_version": 1, "request_id": request_id,
                "owner_generation": owner, "command": name, "payload": payload or {}}
    if session is not None:
        envelope["session_id"] = session
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        connection.connect(str(runtime / "control.sock"))
        connection.sendall(json.dumps(envelope).encode() + b"\n")
        data = bytearray()
        while b"\n" not in data:
            chunk = connection.recv(65536)
            assert chunk, "ack must terminate with a newline"
            data.extend(chunk)
        return json.loads(data.split(b"\n", 1)[0])


def test_stale_owner_cannot_shutdown_current_helper(tmp_path):
    runtime = tmp_path / "runtime"
    with running_helper(runtime) as (process, ready):
        response = command(runtime, "shutdown", owner="old")
        assert response["accepted"] is False
        assert response["error_code"] == "owner_mismatch"
        assert command(runtime, "status", request_id="g1:r2")["status"]["state"] == "idle"
        assert process.poll() is None
        assert ready["owner_generation"] == "g1"


def test_conflicting_duplicate_request_cannot_replace_configuration(tmp_path):
    runtime = tmp_path / "runtime"
    with running_helper(runtime):
        first = command(runtime, "configure", payload={"config_schema": 1, "language": "ru"})
        assert first["accepted"] is True
        assert command(runtime, "configure", payload={"config_schema": 1, "language": "ru"}) == first
        conflict = command(runtime, "configure", payload={"config_schema": 1, "language": "en"})
        assert conflict["accepted"] is False
        assert conflict["error_code"] == "request_conflict"
        status = command(runtime, "status", request_id="g1:r2")
        assert status["status"]["configuration"]["language"] == "ru"


def test_expired_owner_stops_supervisor_and_releases_runtime(tmp_path):
    runtime = tmp_path / "runtime"
    with running_helper(runtime) as (process, ready):
        process.wait(timeout=12)
        assert not (runtime / "control.sock").exists()
    with running_helper(runtime, owner="g2") as (replacement, ready):
        assert ready["owner_generation"] == "g2"
        assert replacement.poll() is None


def test_runtime_directory_and_socket_are_private(tmp_path):
    runtime = tmp_path / "runtime"
    with running_helper(runtime):
        assert runtime.stat().st_mode & 0o777 == 0o700
        assert (runtime / "control.sock").stat().st_mode & 0o777 == 0o600


def test_sigterm_cleans_up_and_old_shutdown_cannot_kill_replacement(tmp_path):
    runtime = tmp_path / "runtime"
    with running_helper(runtime) as (first, ready):
        first.terminate()
        first.wait(timeout=5)
        assert not (runtime / "control.sock").exists()
    with running_helper(runtime, owner="g2") as (second, ready):
        response = command(runtime, "shutdown", owner="g1")
        assert response["accepted"] is False
        assert second.poll() is None


def test_broken_stdout_terminates_helper_instead_of_leaving_a_daemon(tmp_path):
    runtime = tmp_path / "runtime"
    process = subprocess.Popen(
        [sys.executable, "-m", "voiceprompt.cli", "supervise", "--owner", "g1",
         "--runtime-dir", str(runtime)], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    process.stdout.close()
    try:
        process.wait(timeout=5)
        assert not (runtime / "control.sock").exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stderr.close()
