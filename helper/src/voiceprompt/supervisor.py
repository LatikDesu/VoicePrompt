"""Private Unix command server and bounded, nonblocking protocol event stream."""

import asyncio
import hashlib
import importlib.metadata
import json
import os
import signal
import socket
import stat
import struct
import sys
import time
from collections import OrderedDict
from pathlib import Path

from .ownership import Lease, OwnerLock, OwnershipError
from .protocol import MAX_MESSAGE_BYTES, Command, ProtocolError, decode_request, encode_message
from .private_files import DataError, HistoryStore, PrivateFiles, atomic_private_write
from .model_download import ModelDownload, PROFILES
from .hotkeys import EvdevAdapter


# Never serialized behind a slow mutator's dispatch lock.
_LOCK_FREE_COMMANDS = frozenset({"renew_lease", "status"})


class Supervisor:
    def __init__(self, runtime_dir: Path, owner: str, engine=None) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.owner = owner
        self.engine = engine
        self.lease = Lease(owner, time.monotonic())
        self.seq = 0
        self.config = {"config_schema": 1, "language": "ru", "device": "cpu"}
        self.stop_requested = asyncio.Event()
        self.events = asyncio.Queue(maxsize=64)
        self.command_lock = asyncio.Lock()
        self.request_cache = OrderedDict()
        self.inflight: dict[str, tuple[bytes, asyncio.Future]] = {}
        self.clients = set()
        self.server = None
        self.lock = None
        self.socket_identity = None
        self.closed = False
        self.private_files = None
        self.download = ModelDownload(self.emit)
        self.hotkeys = None

    def emit(self, event: str, **fields) -> None:
        if self.closed:
            return
        if self.events.full():
            if event in {"partial", "heartbeat"}:
                return
            self.stop_requested.set()
            raise ProtocolError("event_backpressure")
        self.seq += 1
        self.events.put_nowait(encode_message({
            **fields, "event": event, "protocol_version": 1,
            "owner_generation": self.owner, "seq": self.seq,
        }))

    async def _write_events(self) -> None:
        loop = asyncio.get_running_loop()
        fd = sys.stdout.fileno()
        os.set_blocking(fd, False)
        try:
            while True:
                message = memoryview(await self.events.get())
                while message:
                    try:
                        written = os.write(fd, message)
                        message = message[written:]
                    except BlockingIOError:
                        writable = loop.create_future()
                        loop.add_writer(fd, lambda: None if writable.done() else writable.set_result(None))
                        try:
                            await writable
                        finally:
                            loop.remove_writer(fd)
                self.events.task_done()
        except (BrokenPipeError, ConnectionError, OSError):
            self.stop_requested.set()

    async def _watch_owner(self) -> None:
        heartbeat_at = time.monotonic() + 2
        while not self.stop_requested.is_set():
            await asyncio.sleep(0.25)
            now = time.monotonic()
            if self.lease.expired(now):
                self.stop_requested.set()
                return
            if now >= heartbeat_at:
                self.emit("heartbeat")
                heartbeat_at = now + 2
            try:
                self.private_files.expire()
                await self.download.tick()
            except Exception:
                self.emit("error", error_code="helper_maintenance_failed")
                self.stop_requested.set()
                return
            if self.engine is not None:
                try:
                    await self.engine.tick(now)
                except Exception:
                    self.emit("error", error_code="engine_failed")
                    self.stop_requested.set()
                    return

    def status(self) -> dict:
        state = self.engine.status() if self.engine is not None else {"state": "idle", "model_loaded": False}
        return {**state, "configuration": {
            "language": self.config["language"], "device": self.config["device"],
        }}

    def _ack(self, command: Command | None, accepted: bool, **fields) -> dict:
        return {"protocol_version": 1, "owner_generation": self.owner,
                "request_id": command.request_id if command else None,
                "accepted": accepted, **fields}

    async def handle(self, command: Command) -> dict:
        if command.owner_generation != self.owner:
            return self._ack(command, False, error_code="owner_mismatch")
        fingerprint = hashlib.sha256(encode_message({
            "command": command.command, "session_id": command.session_id,
            "payload": command.payload,
        })).digest()
        cached = self.request_cache.get(command.request_id)
        if cached is not None:
            if cached[0] != fingerprint:
                return self._ack(command, False, error_code="request_conflict")
            return cached[1]
        # Reserve the request identity before any await, so a retry arriving
        # while a slow mutator is still in flight can neither execute twice nor
        # silently change the reply for that request_id.
        pending = self.inflight.get(command.request_id)
        if pending is not None:
            if pending[0] != fingerprint:
                return self._ack(command, False, error_code="request_conflict")
            return await asyncio.shield(pending[1])
        if command.command in _LOCK_FREE_COMMANDS:
            # Renewal and status stay serviceable while a stop/cancel is still
            # finalizing; they mutate neither config nor session state.
            if self.lease.expired(time.monotonic()):
                self.stop_requested.set()
                response = self._ack(command, False, error_code="lease_expired")
            elif command.command == "renew_lease":
                renewed = self.lease.renew(command.owner_generation, time.monotonic())
                response = self._ack(command, renewed, **({} if renewed else {"error_code": "lease_expired"}))
            else:
                response = self._ack(command, True, status=self.status())
            self._remember(command.request_id, fingerprint, response)
            return response
        settled = asyncio.get_running_loop().create_future()
        self.inflight[command.request_id] = (fingerprint, settled)
        response = None
        try:
            try:
                async with self.command_lock:
                    if self.lease.expired(time.monotonic()):
                        self.stop_requested.set()
                        response = self._ack(command, False, error_code="lease_expired")
                    else:
                        response = await self._dispatch(command)
            except (ProtocolError, ValueError) as error:
                response = self._ack(
                    command, False, error_code=getattr(error, "code", "configuration_invalid"))
            self._remember(command.request_id, fingerprint, response)
            return response
        finally:
            self.inflight.pop(command.request_id, None)
            if not settled.done():
                if response is None:
                    settled.cancel()
                else:
                    settled.set_result(response)

    def _remember(self, request_id: str, fingerprint: bytes, response: dict) -> None:
        self.request_cache[request_id] = (fingerprint, response)
        if len(self.request_cache) > 256:
            self.request_cache.popitem(last=False)

    async def _dispatch(self, command: Command) -> dict:
        name = command.command
        if name == "configure":
            payload = command.payload
            if type(payload.get("config_schema")) is not int or payload["config_schema"] != 1:
                raise ProtocolError("config_incompatible")
            next_config = {**self.config, **payload}
            if next_config.get("language") not in {"ru", "en", "auto"} or next_config.get("device") not in {"cpu", "cuda"}:
                raise ProtocolError("configuration_invalid")
            if self.engine is not None:
                self.engine.configure(next_config)
            keyboard_keys = ("evdev_enabled", "evdev_device", "evdev_chord")
            keyboard_changed = any(next_config.get(key) != self.config.get(key) for key in keyboard_keys)
            self.config = next_config
            if self.hotkeys is not None and (keyboard_changed or not next_config.get("evdev_enabled")):
                await self.hotkeys.close()
                self.hotkeys = None
            if next_config.get("evdev_enabled") and self.hotkeys is None:
                self.hotkeys = EvdevAdapter(Path(next_config.get("evdev_device", "")),
                                           next_config.get("evdev_chord", ""), self.emit)
                await self.hotkeys.start()
            self.emit("configured", request_id=command.request_id)
            return self._ack(command, True)
        if name == "model_download":
            profile = command.payload.get("profile", "cpu-small")
            if profile not in PROFILES:
                raise ProtocolError("model_profile_download_unavailable")
            repo, revision = PROFILES[profile]
            cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "voiceprompt" / "models"
            await self.download.start(repo, revision, cache / profile)
            return self._ack(command, True)
        if name == "model_download_cancel":
            await self.download.close()
            return self._ack(command, True)
        if name == "prepare_private_file":
            allocation = self.private_files.allocate(command.payload["purpose"], command.payload["max_bytes"])
            return self._ack(command, True, **allocation)
        if name == "discard_private_file":
            self.private_files.discard(command.payload["token"])
            return self._ack(command, True)
        if name in {"history_save", "history_load", "history_delete", "history_clear"}:
            store = HistoryStore(Path(command.payload["root"]))
            if name == "history_save":
                contents = json.loads(self.private_files.consume(command.payload["token"]))
                if not isinstance(contents, dict) or contents.get("schema") != 1:
                    raise DataError("schema_incompatible")
                store.save(contents.get("records"))
                return self._ack(command, True)
            if name == "history_delete":
                store.delete_record(command.payload["session_id"])
                return self._ack(command, True)
            if name == "history_clear":
                store.clear()
                return self._ack(command, True)
            records = store.load()
            allocation = self.private_files.allocate("history", 8388608)
            snapshot = json.dumps({"schema": 1, "records": records}, ensure_ascii=False, allow_nan=False).encode("utf-8")
            atomic_private_write(Path(allocation["path"]), snapshot)
            return self._ack(command, True, **allocation, count=len(records), schema=1)
        if name == "shutdown":
            self.stop_requested.set()
            return self._ack(command, True)
        if name in {"stop", "cancel", "unload"} and self.engine is None:
            return self._ack(command, True)
        if name in {"start", "stop", "cancel", "unload"}:
            if self.engine is None:
                return self._ack(command, False, error_code="capture_unavailable")
            if name == "unload":
                await self.engine.unload()
            else:
                await getattr(self.engine, name)(command.session_id)
            return self._ack(command, True)
        return self._ack(command, False, error_code="unsupported_command")

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self.clients.add(task)
        command = None
        try:
            connection = writer.get_extra_info("socket")
            credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _, uid, _ = struct.unpack("3i", credentials)
            if uid != os.getuid():
                response = self._ack(None, False, error_code="peer_uid_mismatch")
            else:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=3)
                    command = decode_request(line)
                    response = await self.handle(command)
                except (ValueError, ProtocolError) as error:
                    response = self._ack(command, False, error_code=getattr(error, "code", "message_too_large"))
            writer.write(encode_message(response))
            await asyncio.wait_for(writer.drain(), timeout=3)
        except (OSError, asyncio.TimeoutError, ConnectionError):
            return
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ConnectionError):
                pass
            self.clients.discard(task)

    async def run(self) -> None:
        background = []
        loop = asyncio.get_running_loop()
        path = self.runtime_dir / "control.sock"
        try:
            self.lock = OwnerLock.acquire(self.runtime_dir, self.owner)
            self.private_files = PrivateFiles(self.runtime_dir / "staging")
            self.lease.now = time.monotonic()
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise OwnershipError("insecure_socket_path")
                path.unlink()
            self.server = await asyncio.start_unix_server(self._client, path=str(path), limit=MAX_MESSAGE_BYTES + 1, backlog=8)
            os.chmod(path, 0o600)
            info = path.lstat()
            self.socket_identity = (info.st_dev, info.st_ino)
            for signum in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(signum, self.stop_requested.set)
            background = [asyncio.create_task(self._write_events()), asyncio.create_task(self._watch_owner())]
            capabilities = ["control", "private_files", "history", "model_setup", "evdev_optional"] + (list(self.engine.capabilities) if self.engine is not None else [])
            self.emit("ready", helper_version=importlib.metadata.version("voiceprompt-helper"),
                      protocol_min=1, protocol_max=1, config_schema=1, capabilities=capabilities)
            await self.stop_requested.wait()
        finally:
            self.closed = True
            await self.download.close()
            if self.hotkeys is not None:
                await self.hotkeys.close()
            if self.private_files is not None:
                self.private_files.close()
            if self.engine is not None:
                await self.engine.close()
            if self.server is not None:
                self.server.close()
                await self.server.wait_closed()
            for task in list(self.clients) + background:
                task.cancel()
            await asyncio.gather(*list(self.clients), *background, return_exceptions=True)
            if self.socket_identity is not None:
                try:
                    info = path.lstat()
                    if (info.st_dev, info.st_ino) == self.socket_identity:
                        path.unlink()
                except FileNotFoundError:
                    pass
            if self.lock is not None:
                self.lock.close()
            for signum in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(signum)


def send_command(runtime_dir: Path, envelope: dict, timeout: float = 3.0) -> dict:
    request = encode_message(envelope)
    decode_request(request)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(Path(runtime_dir) / "control.sock"))
        connection.sendall(request)
        response = bytearray()
        while b"\n" not in response:
            chunk = connection.recv(min(65536, MAX_MESSAGE_BYTES + 1 - len(response)))
            if not chunk:
                raise ProtocolError("ack_incomplete")
            response.extend(chunk)
            if len(response) > MAX_MESSAGE_BYTES:
                raise ProtocolError("message_too_large")
        return json.loads(response.split(b"\n", 1)[0])
