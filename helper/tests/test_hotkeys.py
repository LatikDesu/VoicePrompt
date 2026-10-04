import asyncio

import pytest

from voiceprompt.hotkeys import ChordGate, normalize_chord


def test_modifier_release_stops_once_and_repeat_cannot_restart():
    gate = ChordGate(main_key=183, modifiers=frozenset({29}))
    assert gate.feed(29, 1) is None
    assert gate.feed(183, 1) == "pressed"
    assert gate.feed(183, 2) is None
    assert gate.feed(29, 0) == "released"
    assert gate.feed(183, 0) is None


def test_unplug_releases_active_chord_once():
    gate = ChordGate(main_key=183, modifiers=frozenset())
    assert gate.feed(183, 1) == "pressed"
    assert gate.disconnect() == "released"
    assert gate.disconnect() is None


def test_already_held_main_key_requires_fresh_press():
    gate = ChordGate(main_key=183, modifiers=frozenset())
    gate.arm({183})
    assert gate.feed(183, 2) is None
    assert gate.feed(183, 1) is None
    assert gate.feed(183, 0) is None
    assert gate.feed(183, 1) == "pressed"


def test_modifier_arriving_after_main_key_does_not_start_recording():
    gate = ChordGate(main_key=183, modifiers=frozenset({29}))
    assert gate.feed(183, 1) is None
    assert gate.feed(29, 1) is None
    assert gate.feed(183, 0) is None
    assert gate.feed(183, 1) == "pressed"


def test_unrelated_keys_never_enter_tracked_state():
    gate = ChordGate(main_key=183, modifiers=frozenset({29}))
    assert gate.feed(30, 1) is None
    assert gate.held == set()


def test_unassigned_hotkey_never_opens_keyboard(tmp_path):
    import asyncio
    from voiceprompt.hotkeys import EvdevAdapter

    async def scenario():
        events = []
        adapter = EvdevAdapter(tmp_path / "absent", "", lambda event, **fields: events.append((event, fields)))
        assert await adapter.start() is False
        assert events == [("input_unavailable", {"error_code": "hotkey_not_configured"})]
        assert adapter.device is None
        await adapter.close()

    asyncio.run(scenario())


def test_human_readable_chord_activates_real_chord_gate(monkeypatch, tmp_path):
    import asyncio
    import sys
    from types import SimpleNamespace
    from voiceprompt.hotkeys import EvdevAdapter

    class Keyboard:
        def __init__(self, path):
            pass
        def active_keys(self):
            return []
        async def async_read_loop(self):
            await asyncio.sleep(60)
            if False:
                yield
        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "evdev", SimpleNamespace(
        InputDevice=Keyboard, ecodes=SimpleNamespace(ecodes={"KEY_LEFTCTRL": 29, "KEY_SPACE": 57})))

    async def scenario():
        adapter = EvdevAdapter(tmp_path / "keyboard", "Ctrl + Space", lambda event, **fields: None)
        try:
            assert await adapter.start() is True
            assert adapter.gate.feed(29, 1) is None
            assert adapter.gate.feed(57, 1) == "pressed"
            assert adapter.gate.feed(57, 0) == "released"
        finally:
            await adapter.close()

    asyncio.run(scenario())


def _fake_ecodes():
    return {
        "KEY_LEFTCTRL": 29, "KEY_RIGHTCTRL": 97, "KEY_LEFTALT": 56,
        "KEY_LEFTSHIFT": 42, "KEY_LEFTMETA": 125,
        "KEY_SPACE": 57, "KEY_ENTER": 28, "KEY_ESC": 1,
        "KEY_A": 30, "KEY_1": 2, "KEY_F13": 183,
    }


def test_friendly_chord_is_case_and_whitespace_insensitive():
    assert normalize_chord("  CTRL  +  space ", _fake_ecodes()) == (57, frozenset({29}))
    assert normalize_chord("Control+Space", _fake_ecodes()) == (57, frozenset({29}))
    assert normalize_chord("super+Enter", _fake_ecodes()) == (28, frozenset({125}))


def test_letters_digits_and_named_keys_resolve():
    assert normalize_chord("Ctrl+A", _fake_ecodes()) == (30, frozenset({29}))
    assert normalize_chord("Alt+1", _fake_ecodes()) == (2, frozenset({56}))
    assert normalize_chord("Space", _fake_ecodes()) == (57, frozenset())
    assert normalize_chord("Escape", _fake_ecodes()) == (1, frozenset())


def test_explicit_device_key_identifiers_are_accepted_verbatim():
    assert normalize_chord("KEY_LEFTCTRL+KEY_F13", _fake_ecodes()) == (183, frozenset({29}))


def test_empty_unknown_duplicate_and_modifier_only_chords_are_rejected():
    for chord in ("", "   ", "Ctrl+", "+Space", "Ctrl+Nope", "Ctrl+Ctrl",
                  "Ctrl+Shift", "Space+A", "Ctrl+KEY_MISSING"):
        with pytest.raises(ValueError):
            normalize_chord(chord, _fake_ecodes())


class _Event:
    def __init__(self, type, code, value=0):
        self.type = type
        self.code = code
        self.value = value


class _Keyboard:
    def __init__(self, events, held=()):
        self.events = list(events)
        self.held = set(held)
        self.closed = False

    def active_keys(self):
        return sorted(self.held)

    async def async_read_loop(self):
        for event in self.events:
            yield event
        await asyncio.sleep(3600)

    def close(self):
        self.closed = True


def test_syn_dropped_discards_events_until_syn_report():
    from pathlib import Path
    from types import SimpleNamespace
    from voiceprompt.hotkeys import EvdevAdapter

    EV_SYN, EV_KEY, SYN_REPORT, SYN_DROPPED = 0, 1, 0, 3
    ecodes = SimpleNamespace(EV_SYN=EV_SYN, EV_KEY=EV_KEY, SYN_REPORT=SYN_REPORT,
                             SYN_DROPPED=SYN_DROPPED, ecodes={})
    keyboard = _Keyboard([
        _Event(EV_SYN, SYN_DROPPED),
        _Event(EV_KEY, 29, 1), _Event(EV_KEY, 57, 1),  # invalid payload
        _Event(EV_SYN, SYN_REPORT),
        _Event(EV_KEY, 57, 0), _Event(EV_KEY, 29, 0),  # kernel snapshot: nothing held
        _Event(EV_KEY, 29, 1), _Event(EV_KEY, 57, 1),  # fresh, genuine press
    ])

    async def scenario():
        reported = []
        adapter = EvdevAdapter(Path("keyboard"), "Ctrl+Space",
                               lambda event, **fields: reported.append(event))
        adapter.device = keyboard
        adapter.gate = ChordGate(57, frozenset({29}))
        task = asyncio.create_task(adapter._read(ecodes))
        try:
            await asyncio.sleep(0)
            assert reported == ["hotkey_pressed"], (
                "events between SYN_DROPPED and SYN_REPORT must be discarded")
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())
