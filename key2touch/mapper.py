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


LOOK_SCALE = 0.0005  # touch movement (fraction of output height) per mouse count at sens 1.0
LOOK_IDLE = 0.3  # seconds without mouse motion before the camera finger lifts


class Look:
    """Mouse motion → a dragging finger, restarted when it reaches the edge.

    Restarting is the delicate part: Android hands the new finger the same
    pointer id, so if the lift and the new press land in one game frame the
    game sees that pointer *jump* back across the area and the camera jerks.
    So after lifting we wait a couple of frames (buffering the motion), and
    start the next stroke on the far side of the area to make each stroke as
    long as possible."""

    GAP = 0.02  # s between lift and the next press: just over one 60 fps frame
    RESTART = 0.7  # next stroke starts this fraction of r behind the centre

    def __init__(self, touch, pt, aspect):
        self.touch, self.cx, self.cy = touch, pt["x"], pt["y"]
        self.r = pt.get("r", 0.22)
        self.k = pt.get("sens", 1.0) * LOOK_SCALE
        self.aspect = aspect
        self.slot = None
        self.ox = self.oy = 0.0  # finger offset from the centre, in height units
        self.last = 0.0
        self.resume_at = None  # set while waiting between strokes
        self.px = self.py = 0.0  # motion buffered during that wait

    def motion(self, dx, dy, now):
        """dx, dy: mouse counts accumulated over one read batch."""
        if not dx and not dy:
            return
        self.last = now
        self.px += dx * self.k
        self.py += dy * self.k
        self._flush(now)

    def _flush(self, now):
        restart = False
        if self.resume_at is not None:
            if now < self.resume_at:
                return  # still in the gap: keep buffering
            self.resume_at = None
            restart = True
        mx, my, self.px, self.py = self.px, self.py, 0.0, 0.0
        if not mx and not my:
            return
        if self.slot is None:
            n = math.hypot(mx, my)
            if restart:  # begin behind the centre, opposite to the motion
                self.ox, self.oy = -mx / n * self.r * self.RESTART, -my / n * self.r * self.RESTART
            else:
                self.ox = self.oy = 0.0
            self.slot = self.touch.down(self.cx + self.ox / self.aspect, self.cy + self.oy)
            if self.slot is None:
                return
        nx, ny = self.ox + mx, self.oy + my
        if nx * nx + ny * ny <= self.r * self.r:
            self._to(nx, ny)
            return
        # Crosses the edge: go exactly to it, lift, and carry the rest over to
        # the next stroke — no motion is lost however small the area is.
        a = mx * mx + my * my
        b = 2 * (self.ox * mx + self.oy * my)
        c = self.ox * self.ox + self.oy * self.oy - self.r * self.r
        t = max(0.0, min(1.0, (-b + math.sqrt(max(0.0, b * b - 4 * a * c))) / (2 * a)))
        self._to(self.ox + t * mx, self.oy + t * my)
        self.release()
        self.resume_at = now + self.GAP
        self.px, self.py = (1 - t) * mx, (1 - t) * my

    def _to(self, ox, oy):
        self.ox, self.oy = ox, oy
        self.touch.move(self.slot, self.cx + ox / self.aspect, self.cy + oy)

    def idle(self, now):
        if self.resume_at is not None:
            self._flush(now)
            return
        if self.slot is not None and now - self.last >= LOOK_IDLE:
            self.release()

    def deadline(self):
        if self.resume_at is not None:
            return self.resume_at
        return self.last + LOOK_IDLE if self.slot is not None else None

    def release(self):
        if self.slot is not None:
            self.touch.up(self.slot)
            self.slot = None

    def reset(self):
        """Stop entirely (camera mode left): no pending stroke."""
        self.release()
        self.resume_at = None
        self.px = self.py = 0.0


class Mapper:
    def __init__(self, touch, profile, aspect=16 / 9):
        self.controls = []
        self.bindings = {}  # keycode -> [(mods frozenset, control index)]
        self.look = None
        self.cursor_key = None
        self.cursor_mode = "toggle"
        for i, pt in enumerate(profile.get("points", [])):
            if pt.get("type") == "look":
                c = Look(touch, pt, aspect)
                binds = []
                if self.look is None:  # one camera area per profile
                    self.look = c
                    self.cursor_key = keycode(pt.get("cursor_key"))
                    self.cursor_mode = pt.get("cursor_mode", "toggle")
            elif pt.get("type") == "joystick":
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
            c.reset() if isinstance(c, Look) else c.release()
