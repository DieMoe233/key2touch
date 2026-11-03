"""Key → touch state machine. Pure logic; the touch backend is injected."""

import math
from evdev import ecodes as e

from .profile import JOY_DIRS

# Modifier keys, grouped so either side matches ("CTRL" = left or right Ctrl).
MOD_GROUP = {
    e.KEY_LEFTCTRL: "CTRL", e.KEY_RIGHTCTRL: "CTRL",
    e.KEY_LEFTSHIFT: "SHIFT", e.KEY_RIGHTSHIFT: "SHIFT",
    e.KEY_LEFTALT: "ALT", e.KEY_RIGHTALT: "ALT",
    e.KEY_LEFTMETA: "META", e.KEY_RIGHTMETA: "META",
}
MOD_ORDER = ("CTRL", "ALT", "SHIFT", "META")


def keycode(name):
    if name is None:
        return None
    if isinstance(name, int):
        return name
    return e.ecodes.get(name)


class Tap:
    def __init__(self, touch, pt):
        self.touch, self.x, self.y = touch, pt["x"], pt["y"]
        self.slot = None

    def key(self, code, down):
        if down and self.slot is None:
            self.slot = self.touch.down(self.x, self.y)
        elif not down and self.slot is not None:
            self.touch.up(self.slot)
            self.slot = None

    def release(self):
        if self.slot is not None:
            self.touch.up(self.slot)
            self.slot = None


class Joystick:
    def __init__(self, touch, pt, aspect):
        self.touch, self.x, self.y = touch, pt["x"], pt["y"]
        r = pt.get("r", 0.12)
        self.ry = r
        self.rx = r / aspect  # aspect = width / height
        self.dirs = {}  # keycode -> dir name
        for d in JOY_DIRS:
            c = keycode(pt.get("keys", {}).get(d))
            if c is not None:
                self.dirs[c] = d
        self.held = set()
        self.slot = None

    def target(self):
        dx = ("right" in self.held) - ("left" in self.held)
        dy = ("down" in self.held) - ("up" in self.held)
        n = math.hypot(dx, dy)
        if n == 0:
            return self.x, self.y
        return self.x + dx / n * self.rx, self.y + dy / n * self.ry

    def key(self, code, down):
        d = self.dirs[code]
        if down:
            self.held.add(d)
        else:
            self.held.discard(d)
        if not self.held:
            self.release()
            return
        if self.slot is None:
            # Touch down at the centre first so the game anchors the stick there.
            self.slot = self.touch.down(self.x, self.y)
            if self.slot is None:
                return
        self.touch.move(self.slot, *self.target())

    def release(self):
        self.held.clear()
        if self.slot is not None:
            self.touch.up(self.slot)
            self.slot = None


class Mapper:
    def __init__(self, touch, profile, aspect=16 / 9):
        self.controls = []
        self.bindings = {}  # keycode -> [(mods frozenset, control index)]
        for i, pt in enumerate(profile.get("points", [])):
            if pt.get("type") == "joystick":
                c = Joystick(touch, pt, aspect)
                binds = [(code, frozenset()) for code in c.dirs]
            else:
                c = Tap(touch, pt)
                code = keycode(pt.get("key"))
                binds = [] if code is None else [(code, frozenset(pt.get("mods") or ()))]
            self.controls.append(c)
            for code, mods in binds:
                self.bindings.setdefault(code, []).append((mods, i))
        self.mods_down = set()  # held modifier keycodes (all keyboards)
        self.fired = {}  # keycode -> control index it triggered on press

    def track(self, code, value):
        """Feed every key event (mapped or not, active or not) to keep modifier state."""
        if code in MOD_GROUP:
            if value == 0:
                self.mods_down.discard(code)
            elif value == 1:
                self.mods_down.add(code)

    def mod_groups(self, exclude=None):
        return {MOD_GROUP[c] for c in self.mods_down if c != exclude}

    def key(self, code, value):
        """value: 1 press, 0 release, 2 autorepeat.

        Returns the index of the control that consumed the key, or None if the
        key should pass through.
        """
        if value == 2:
            return self.fired.get(code)
        if value == 0:
            i = self.fired.pop(code, None)
            if i is not None:
                self.controls[i].key(code, False)
            return i
        cands = self.bindings.get(code)
        if not cands:
            return None
        held = self.mod_groups(exclude=code)
        # Most specific match wins: Ctrl+Q beats plain Q when Ctrl is held.
        best = max((m for m in cands if m[0] <= held), key=lambda m: len(m[0]), default=None)
        if best is None:
            return None
        i = best[1]
        self.fired[code] = i
        self.controls[i].key(code, True)
        return i

    def release_all(self):
        self.fired.clear()
        for c in self.controls:
            c.release()
