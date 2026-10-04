"""UTF-8 JSON control envelopes; stdout events use the same byte ceiling."""

import json
from dataclasses import dataclass, field

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 262144
MAX_TEXT_BYTES = 65536
RECORDING_COMMANDS = frozenset({"start", "stop", "cancel"})


class ProtocolError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class Command:
    protocol_version: int
    request_id: str
    owner_generation: str
    command: str
    session_id: str | None = None
    payload: dict = field(default_factory=dict)


def _reject_constant(value: str) -> None:
    raise ValueError("nonfinite_json_constant")


def _identity(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value.encode("utf-8")) <= 256


def decode_request(line: bytes) -> Command:
    if len(line) > MAX_MESSAGE_BYTES:
        raise ProtocolError("message_too_large")
    try:
        value = json.loads(line.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError("invalid_json") from None
    if not isinstance(value, dict):
        raise ProtocolError("invalid_envelope")
    version = value.get("protocol_version")
    if type(version) is not int or version != PROTOCOL_VERSION:
        raise ProtocolError("protocol_incompatible")
    if any(not _identity(value.get(name)) for name in ("request_id", "owner_generation", "command")):
        raise ProtocolError("invalid_identity")
    session = value.get("session_id")
    if value["command"] in RECORDING_COMMANDS and not _identity(session):
        raise ProtocolError("session_required")
    if session is not None and not _identity(session):
        raise ProtocolError("invalid_session")
    payload = value.get("payload", {})
    if not isinstance(payload, dict):
        raise ProtocolError("invalid_payload")
    return Command(version, value["request_id"], value["owner_generation"], value["command"], session, payload)


def encode_message(value: dict) -> bytes:
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ProtocolError("message_too_large")
    return encoded + b"\n"
