"""Unsafe envelopes must never reach capture/control dispatch."""

import json

import pytest

from voiceprompt.protocol import ProtocolError, decode_request


def request(**updates):
    value = {"protocol_version": 1, "request_id": "g1:r1", "owner_generation": "g1", "command": "renew_lease", "payload": {}}
    value.update(updates)
    return json.dumps(value).encode()


def test_oversized_request_rejected_before_json_decode():
    with pytest.raises(ProtocolError, match="message_too_large"):
        decode_request(b"x" * 262145)


@pytest.mark.parametrize("raw", [b"{", b"\xff", b"[]", b"null", b'{"protocol_version":NaN}'])
def test_invalid_json_or_nonobject_cannot_dispatch(raw):
    with pytest.raises(ProtocolError):
        decode_request(raw)


@pytest.mark.parametrize("version", [True, "1", 2, 0])
def test_invalid_or_unknown_protocol_cannot_dispatch(version):
    with pytest.raises(ProtocolError):
        decode_request(request(protocol_version=version))


@pytest.mark.parametrize("field", ["request_id", "owner_generation", "command"])
def test_empty_identity_or_command_cannot_dispatch(field):
    with pytest.raises(ProtocolError):
        decode_request(request(**{field: ""}))


def test_recording_command_requires_session_identity():
    with pytest.raises(ProtocolError, match="session_required"):
        decode_request(request(command="start"))


def test_control_payload_is_a_json_object():
    with pytest.raises(ProtocolError, match="invalid_payload"):
        decode_request(request(payload=["not", "configuration"]))


def test_renewal_does_not_require_a_recording_session():
    command = decode_request(request())
    assert command.command == "renew_lease"
    assert command.session_id is None
