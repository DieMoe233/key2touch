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


def new_joystick(x, y, r=0.12):
    return {
        "type": "joystick", "x": x, "y": y, "r": r,
        "keys": {"up": "KEY_W", "left": "KEY_A", "down": "KEY_S", "right": "KEY_D"},
    }


def new_look(x, y, r=0.22):
    """Camera area: mouse motion drags a virtual finger around (x, y).

    r: how far the finger may wander (fraction of output height) before it
    is lifted and put back at the centre; sens: touch distance per mouse
    count (1.0 = 0.0005 of the output height); cursor_key: brings the mouse
    cursor back — tapped on its own to toggle (cursor_mode "toggle"), or
    held (cursor_mode "hold")."""
    return {"type": "look", "x": x, "y": y, "r": r, "sens": 1.0,
            "cursor_key": "KEY_LEFTCTRL", "cursor_mode": "toggle"}


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
