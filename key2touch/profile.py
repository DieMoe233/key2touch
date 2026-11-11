"""Profile model.

All positions are normalized to the target output (0..1 on both axes).
niri maps an absolute touch device onto the output's *logical* rectangle,
and the HUD layer surface is that same logical rectangle, so normalized
coordinates need no pixel / fractional-scale conversion anywhere.

Joystick radius is a fraction of the output height; the engine scales the
x offset by height/width so the stick is round on screen.
"""

import json
import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "key2touch"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "key2touch"
PROFILE_DIR = CONFIG_DIR / "profiles"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

TAP_R = 0.025  # default tap size, fraction of output height

JOY_DIRS = ("up", "left", "down", "right")

# Gamepad sticks a point can follow; the engine resolves the name to the axes
# each pad really has (ABS_X/Y, ABS_RX/RY, ...).
STICK_NAMES = ("left", "right")
STICK_DEADZONE = 0.15  # fraction of full deflection ignored around the centre

DEFAULT_PROFILE = {
    "output": "",
    "only_focused": False,
    # app_id rules (shell-style wildcards, case-insensitive); any match counts.
    # Waydroid is "Waydroid" in full-UI mode and "waydroid.<package>" per app.
    "windows": ["Waydroid", "waydroid.*"],
    "grab": True,
    "toggle_key": "KEY_F1",
    "toggle_mods": [],
    "edit_key": "KEY_F2",  # opens / closes the layout editor (F11 is Waydroid's fullscreen)
    "edit_mods": [],
    "points": [],
}


def new_tap(x, y, key=None, r=TAP_R):
    return {"type": "tap", "x": x, "y": y, "r": r, "key": key, "mods": []}


# Skill key (MOBA "drag to aim"): button radius used for drawing and hit tests;
# its r is how far the finger may be dragged from the button.
SKILL_BTN = 0.035
SKILL_R = 0.1

# Point types driven by one key (or combo): binding rules treat them alike.
KEYED = ("tap", "skill")


def new_skill(x, y, key=None, r=SKILL_R):
    """Hold the key: a finger presses (x, y) and follows the mouse within r;
    release: it lifts and the skill fires in that direction. A tap without
    moving the mouse is the game's quick cast. While held, the right mouse
    button moves the finger to cancel (x, y) first, so the skill is called
    off instead (cancel None: right button does nothing)."""
    return {"type": "skill", "x": x, "y": y, "r": r, "sens": 1.0, "key": key, "mods": [], "cancel": None}


def new_joystick(x, y, r=0.12, stick=None, keys=None):
    """Touch joystick: 4 direction keys and/or a gamepad stick.

    keys and stick add up, so a D-pad (as BTN_DPAD_*) and the left stick can
    drive the same touch stick; a new point uses WASD unless a stick is asked
    for, in which case it starts with no keys."""
    if keys is None:
        keys = ({"up": None, "left": None, "down": None, "right": None} if stick
                else {"up": "KEY_W", "left": "KEY_A", "down": "KEY_S", "right": "KEY_D"})
    return {
        "type": "joystick", "x": x, "y": y, "r": r, "keys": keys,
        "stick": stick, "deadzone": STICK_DEADZONE,
    }


# Below this the camera finger reaches the edge so often that turning stutters.
LOOK_MIN_R = 0.15


def new_look(x, y, r=0.22, stick=None):
    """Camera area: mouse motion drags a virtual finger around (x, y).

    r: how far the finger may wander (fraction of output height) before it
    is lifted and put back at the centre; sens: touch distance per mouse
    count (1.0 = 0.0005 of the output height); cursor_key: brings the mouse
    cursor back — tapped on its own to toggle (cursor_mode "toggle"), or
    held (cursor_mode "hold"); stick: a gamepad stick that turns the camera
    as well ("left"/"right", or None for the mouse only)."""
    return {"type": "look", "x": x, "y": y, "r": r, "sens": 1.0,
            "cursor_key": "KEY_LEFTCTRL", "cursor_mode": "toggle", "stick": stick}


def normalize(p):
    out = dict(DEFAULT_PROFILE)
    out.update(p or {})
    out["points"] = [dict(pt) for pt in out.get("points", [])]
    out["windows"] = list(out.get("windows") or [])
    return out


def _path(name):
    return PROFILE_DIR / f"{name}.json"


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)


def valid_name(name):
    return bool(name) and "/" not in name and not name.startswith(".")


def list_profiles():
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(f.stem for f in PROFILE_DIR.glob("*.json"))


def exists(name):
    return _path(name).exists()


def load(name):
    path = _path(name)
    if not path.exists():
        return normalize({})
    return normalize(json.loads(path.read_text()))


def save(name, prof):
    _write_json(_path(name), prof)


def delete(name):
    _path(name).unlink(missing_ok=True)


def rename(old, new):
    _path(old).rename(_path(new))


def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_settings(settings):
    _write_json(SETTINGS_FILE, settings)
