"""Gamepad support: recognition, axis normalisation, name tables.

A controller is just another evdev device: its buttons are EV_KEY codes in the
BTN_* range (profiles bind them exactly like keyboard keys) and its sticks are
EV_ABS axes.  Axis codes differ between drivers, so a profile names the stick
("left"/"right") and every device resolves that name to the axes it has.
"""

from evdev import ecodes as e

# Canonical evdev name -> (short name used by the CLI, label for the HUD).
BUTTONS = {
    "BTN_SOUTH": ("south", "A"),
    "BTN_EAST": ("east", "B"),
    "BTN_NORTH": ("north", "Y"),
    "BTN_WEST": ("west", "X"),
    "BTN_TL": ("l1", "LB"),
    "BTN_TR": ("r1", "RB"),
    "BTN_TL2": ("l2", "LT"),
    "BTN_TR2": ("r2", "RT"),
    "BTN_SELECT": ("select", "Select"),
    "BTN_START": ("start", "Start"),
    "BTN_MODE": ("guide", "Guide"),
    "BTN_THUMBL": ("l3", "L3"),
    "BTN_THUMBR": ("r3", "R3"),
    "BTN_DPAD_UP": ("dpad-up", "↑"),
    "BTN_DPAD_DOWN": ("dpad-down", "↓"),
    "BTN_DPAD_LEFT": ("dpad-left", "←"),
    "BTN_DPAD_RIGHT": ("dpad-right", "→"),
}
CODE = {name: e.ecodes[name] for name in BUTTONS}
NAME = {code: name for name, code in CODE.items()}
SHORT = {CODE[name]: short for name, (short, _) in BUTTONS.items()}
LABEL = {CODE[name]: label for name, (_, label) in BUTTONS.items()}
ALIAS = {SHORT[code]: code for code in NAME}
# Shoulder names as printed on most pads.
ALIAS.update({"lb": CODE["BTN_TL"], "rb": CODE["BTN_TR"],
              "lt": CODE["BTN_TL2"], "rt": CODE["BTN_TR2"]})

# Every BTN_* name the kernel knows, for paddles / extra buttons that are not
# in the table above (BTN_TRIGGER_HAPPY*, ...).
RAW = {}
for _name, _code in e.ecodes.items():
    if isinstance(_code, int) and _name.startswith("BTN_"):
        RAW.setdefault(_code, []).append(_name)


def is_button(code):
    """True for the BTN_* codes a controller sends (not mouse buttons)."""
    return (e.BTN_JOYSTICK <= code <= e.BTN_THUMBR
            or e.BTN_DPAD_UP <= code <= e.BTN_DPAD_RIGHT
            or e.BTN_TRIGGER_HAPPY1 <= code <= e.BTN_TRIGGER_HAPPY40)


def code(value):
    """'south', 'dpad-up', 'BTN_SOUTH' or 304 -> 304; unknown -> None."""
    if isinstance(value, int):
        return value
    if not value:
        return None
    text = str(value).strip()
    found = ALIAS.get(text.lower()) or CODE.get(text.upper())
    if found is None and text.upper().startswith("BTN_"):
        raw = e.ecodes.get(text.upper())
        if raw is not None and is_button(raw):
            found = raw
    return found


def name(value):
    """Canonical evdev name ('BTN_SOUTH') or None."""
    c = code(value)
    if c is None or not is_button(c):
        return None
    if c in NAME:
        return NAME[c]
    names = RAW.get(c)
    # Aliases share a code (BTN_TRIGGER_HAPPY == BTN_TRIGGER_HAPPY1): the
    # longest name is the specific one.
    return max(names, key=len) if names else None


def short(value):
    """Short CLI name ('south') or None."""
    c = code(value)
    return SHORT.get(c) if c is not None else None


def label(value):
    """Label for the HUD/editor ('A', 'LB', '↑') or None."""
    c = code(value)
    return LABEL.get(c) if c is not None else None


def stick_axes(absinfo):
    """'left' / 'right' -> (x_code, y_code) for the sticks this device has.

    Most pads use ABS_X/ABS_Y and ABS_RX/ABS_RY.  Others (e.g. the Xbox pad
    over Bluetooth) put the right stick on ABS_Z/ABS_RZ and the triggers on
    ABS_GAS/ABS_BRAKE; there the Z pair is a stick even though its range is
    unsigned.  Without GAS/BRAKE, an unsigned Z pair is a trigger pair, and
    a signed one is a stick (older joysticks)."""
    out = {}
    if e.ABS_X in absinfo and e.ABS_Y in absinfo:
        out["left"] = (e.ABS_X, e.ABS_Y)
    if e.ABS_RX in absinfo and e.ABS_RY in absinfo:
        out["right"] = (e.ABS_RX, e.ABS_RY)
    elif e.ABS_Z in absinfo and e.ABS_RZ in absinfo:
        triggers_elsewhere = e.ABS_GAS in absinfo and e.ABS_BRAKE in absinfo
        if triggers_elsewhere or absinfo[e.ABS_Z].min < 0:
            out["right"] = (e.ABS_Z, e.ABS_RZ)
    return out


def normalize(info, value):
    """Raw axis value -> -1..1, using the range the device reports."""
    span = info.max - info.min
    if span <= 0:
        return 0.0
    v = (value - info.min) / span * 2 - 1
    return -1.0 if v < -1 else 1.0 if v > 1 else v


TRIGGER_PRESS = 0.55  # analogue triggers become buttons at these points;
TRIGGER_RELEASE = 0.35  # the gap stops a trigger resting near it from chattering


class AxisButtons:
    """Buttons a pad only reports as axes.

    Plenty of pads have no BTN_DPAD_* codes (just ABS_HAT0X/Y) and no digital
    trigger buttons (just ABS_Z/ABS_RZ).  feed() turns those axes into the
    button presses a profile can bind, and the engine stops forwarding the
    raw axis, so the game sees the same thing it would from a pad that has
    the buttons."""

    def __init__(self, absinfo, keys, sticks=None):
        self.hat = {}
        if e.BTN_DPAD_UP not in keys:
            if e.ABS_HAT0X in absinfo:
                self.hat[e.ABS_HAT0X] = {e.BTN_DPAD_LEFT: -1, e.BTN_DPAD_RIGHT: 1}
            if e.ABS_HAT0Y in absinfo:
                self.hat[e.ABS_HAT0Y] = {e.BTN_DPAD_UP: -1, e.BTN_DPAD_DOWN: 1}
        used = {c for pair in (sticks or {}).values() for c in pair}
        self.triggers = {}
        for axis, btn in ((e.ABS_Z, e.BTN_TL2), (e.ABS_RZ, e.BTN_TR2),
                          (e.ABS_GAS, e.BTN_TL2), (e.ABS_BRAKE, e.BTN_TR2)):
            info = absinfo.get(axis)
            if (info is not None and btn not in keys and axis not in used
                    and info.min >= 0 and info.max > info.min):
                self.triggers[axis] = (btn, info)
        self.down = set()

    @property
    def axes(self):
        """Axes whose raw value is replaced by the buttons below."""
        return set(self.hat) | set(self.triggers)

    def codes(self):
        """Every button code this can press (the mirror needs them)."""
        return {b for m in self.hat.values() for b in m} | {b for b, _ in self.triggers.values()}

    def feed(self, axis, value):
        """-> list of (button code, down) changes caused by this axis value."""
        out = []
        if axis in self.hat:
            for btn, sign in self.hat[axis].items():
                want = value == sign
                if want != (btn in self.down):
                    (self.down.add if want else self.down.discard)(btn)
                    out.append((btn, want))
        elif axis in self.triggers:
            btn, info = self.triggers[axis]
            v = (value - info.min) / (info.max - info.min)
            if btn not in self.down and v >= TRIGGER_PRESS:
                self.down.add(btn)
                out.append((btn, True))
            elif btn in self.down and v <= TRIGGER_RELEASE:
                self.down.discard(btn)
                out.append((btn, False))
        return out
