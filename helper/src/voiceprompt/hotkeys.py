"""Opt-in evdev observation of one chord. No grab, permissions changes or key logs."""

import asyncio
from pathlib import Path

_MODIFIER_ALIASES = {
    "ctrl": "KEY_LEFTCTRL",
    "control": "KEY_LEFTCTRL",
    "alt": "KEY_LEFTALT",
    "shift": "KEY_LEFTSHIFT",
    "super": "KEY_LEFTMETA",
    "win": "KEY_LEFTMETA",
    "meta": "KEY_LEFTMETA",
}

_MODIFIER_KEYS = frozenset({
    "KEY_LEFTCTRL", "KEY_RIGHTCTRL", "KEY_LEFTALT", "KEY_RIGHTALT",
    "KEY_LEFTSHIFT", "KEY_RIGHTSHIFT", "KEY_LEFTMETA", "KEY_RIGHTMETA",
})

_NAMED_KEYS = {
    "space": "KEY_SPACE", "spacebar": "KEY_SPACE",
    "enter": "KEY_ENTER", "return": "KEY_ENTER",
    "tab": "KEY_TAB",
    "escape": "KEY_ESC", "esc": "KEY_ESC",
    "backspace": "KEY_BACKSPACE",
    "delete": "KEY_DELETE", "del": "KEY_DELETE",
    "insert": "KEY_INSERT", "ins": "KEY_INSERT",
    "home": "KEY_HOME", "end": "KEY_END",
    "pageup": "KEY_PAGEUP", "pgup": "KEY_PAGEUP",
    "pagedown": "KEY_PAGEDOWN", "pgdn": "KEY_PAGEDOWN",
    "up": "KEY_UP", "down": "KEY_DOWN", "left": "KEY_LEFT", "right": "KEY_RIGHT",
    "capslock": "KEY_CAPSLOCK", "scrolllock": "KEY_SCROLLLOCK",
    "printscreen": "KEY_SYSRQ", "pause": "KEY_PAUSE", "menu": "KEY_MENU",
    "minus": "KEY_MINUS", "equal": "KEY_EQUAL",
    "comma": "KEY_COMMA", "period": "KEY_DOT", "dot": "KEY_DOT",
    "slash": "KEY_SLASH", "semicolon": "KEY_SEMICOLON",
    "apostrophe": "KEY_APOSTROPHE", "grave": "KEY_GRAVE", "backslash": "KEY_BACKSLASH",
}


def _resolve_token(token: str, ecodes: dict) -> tuple[int, bool] | None:
    """Map one chord token to (evdev keycode, is_modifier), or None if unknown."""
    lowered = token.lower()
    name = _MODIFIER_ALIASES.get(lowered)
    if name is not None:
        code = ecodes.get(name)
        return (code, True) if type(code) is int else None
    if lowered in _NAMED_KEYS:
        name = _NAMED_KEYS[lowered]
    elif len(lowered) == 1 and lowered.isascii() and lowered.isalnum():
        name = "KEY_" + lowered.upper()
    elif lowered.startswith("f") and lowered[1:].isdigit():
        name = "KEY_F" + lowered[1:]
    elif token.upper().startswith("KEY_"):
        name = token.upper()
    else:
        return None
    code = ecodes.get(name)
    if type(code) is not int:
        return None
    return code, name in _MODIFIER_KEYS


def normalize_chord(chord: str, ecodes: dict) -> tuple[int, frozenset[int]]:
    """Resolve a user chord to (main keycode, modifier keycodes). Raises ValueError."""
    tokens = [token.strip() for token in chord.split("+")]
    if not tokens or any(not token for token in tokens):
        raise ValueError("empty chord token")
    resolved: list[tuple[int, bool]] = []
    for token in tokens:
        item = _resolve_token(token, ecodes)
        if item is None:
            raise ValueError("unknown chord token")
        resolved.append(item)
    codes = [code for code, _ in resolved]
    if len(set(codes)) != len(codes):
        raise ValueError("duplicate chord key")
    main_key = codes[-1]
    if resolved[-1][1]:
        raise ValueError("chord has no main key")
    if not all(is_modifier for _, is_modifier in resolved[:-1]):
        raise ValueError("non-modifier key used as modifier")
    return main_key, frozenset(codes[:-1])


class ChordGate:
    def __init__(self, main_key: int, modifiers: frozenset[int]) -> None:
        self.main_key = main_key
        self.modifiers = modifiers
        self.selected = modifiers | {main_key}
        self.held = set()
        self.active = False

    def arm(self, held) -> None:
        self.held = set(held).intersection(self.selected)
        self.active = False

    def feed(self, key: int, value: int) -> str | None:
        if key not in self.selected or value == 2:
            return None
        if value == 1:
            fresh = key not in self.held
            self.held.add(key)
            if fresh and key == self.main_key and not self.active and self.modifiers.issubset(self.held):
                self.active = True
                return "pressed"
        elif value == 0:
            self.held.discard(key)
            if self.active:
                self.active = False
                return "released"
        return None

    def disconnect(self) -> str | None:
        active = self.active
        self.active = False
        self.held.clear()
        return "released" if active else None


class EvdevAdapter:
    def __init__(self, device_path: Path, chord: str, publish) -> None:
        self.device_path = Path(device_path)
        self.chord = chord
        self.publish = publish
        self.device = None
        self.gate = None
        self.task = None

    async def start(self) -> bool:
        chord = self.chord if isinstance(self.chord, str) else ""
        if not chord.strip():
            self.publish("input_unavailable", error_code="hotkey_not_configured")
            return False
        try:
            import evdev
        except ImportError:
            self.publish("input_unavailable", error_code="evdev_not_installed")
            return False
        try:
            main_key, modifiers = normalize_chord(chord, evdev.ecodes.ecodes)
        except (AttributeError, KeyError, TypeError, ValueError):
            self.publish("input_unavailable", error_code="hotkey_invalid")
            return False
        self.gate = ChordGate(main_key, modifiers)
        try:
            self.device = evdev.InputDevice(str(self.device_path))
            self.gate.arm(self.device.active_keys())
        except (OSError, PermissionError):
            self.publish("input_unavailable", error_code="evdev_access_denied")
            return False
        self.task = asyncio.create_task(self._read(evdev.ecodes))
        return True

    async def _read(self, ecodes) -> None:
        resynchronizing = False
        try:
            async for event in self.device.async_read_loop():
                if resynchronizing:
                    # After an overflow every queued event is unreliable; the Linux
                    # input contract discards them through the next SYN_REPORT and
                    # only then re-reads the authoritative key state.
                    if event.type == ecodes.EV_SYN and event.code == ecodes.SYN_REPORT:
                        resynchronizing = False
                        self.gate.arm(self.device.active_keys())
                    continue
                if event.type == ecodes.EV_SYN and event.code == ecodes.SYN_DROPPED:
                    if self.gate.disconnect():
                        self.publish("hotkey_released")
                    resynchronizing = True
                elif event.type == ecodes.EV_KEY:
                    transition = self.gate.feed(event.code, event.value)
                    if transition:
                        self.publish("hotkey_" + transition)
        except OSError:
            if self.gate.disconnect():
                self.publish("hotkey_released")
            self.publish("input_unavailable", error_code="evdev_device_removed")
        finally:
            if self.device is not None:
                self.device.close()
                self.device = None

    async def close(self) -> None:
        if self.gate and self.gate.disconnect():
            self.publish("hotkey_released")
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.device is not None:
            self.device.close()
            self.device = None
