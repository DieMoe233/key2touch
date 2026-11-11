"""Key → touch state machine. Pure logic; the touch backend is injected."""

import math
from evdev import ecodes as e

from .gamepad import is_button as is_gamepad_button
from .profile import JOY_DIRS, LOOK_MIN_R, SKILL_R, STICK_DEADZONE, STICK_NAMES

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
    """Touch stick driven by direction keys and/or a gamepad stick.

    Keys are 8-way; an analogue stick keeps its magnitude, so walking slowly
    is possible. Its deadzone is rescaled rather than just dropped: just
    outside it the finger starts from the centre, and full deflection still
    reaches the rim."""

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
        self.deadzone = float(pt.get("deadzone", STICK_DEADZONE))
        self.ax = self.ay = 0.0  # analogue deflection, -1..1
        self.held = set()
        self.slot = None

    def axis(self, x, y):
        """Analogue stick deflection (-1..1 on both axes)."""
        mag = math.hypot(x, y)
        if mag <= self.deadzone:
            self.ax = self.ay = 0.0
        else:
            scale = min(1.0, (mag - self.deadzone) / (1.0 - self.deadzone)) / mag
            self.ax, self.ay = x * scale, y * scale
        self._apply()

    def vector(self):
        """Combined deflection of keys and stick; None when nothing is held."""
        dx = ("right" in self.held) - ("left" in self.held) + self.ax
        dy = ("down" in self.held) - ("up" in self.held) + self.ay
        n = math.hypot(dx, dy)
        if n == 0:
            return (0.0, 0.0) if self.held else None
        if n > 1:  # keys and a full stick push add up: stay inside the rim
            dx, dy = dx / n, dy / n
        return dx, dy

    def target(self):
        v = self.vector()
        if v is None:
            return self.x, self.y
        return self.x + v[0] * self.rx, self.y + v[1] * self.ry

    def key(self, code, down):
        d = self.dirs[code]
        if down:
            self.held.add(d)
        else:
            self.held.discard(d)
        self._apply()

    def _apply(self):
        if not self.held and self.ax == 0 and self.ay == 0:
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
STICK_DEAD = 0.1  # stick drift ignored when steering the camera or aiming a skill


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
        self.r = max(pt.get("r", 0.22), LOOK_MIN_R)  # older profiles may hold smaller values
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


class Skill:
    """Drag-to-aim skill button (MOBA games).

    Key down presses the button; while it is held, mouse motion moves the
    finger around it, never further than r (past the edge the offset is
    projected back onto the circle, so reversing the mouse reacts at once).
    Key up lifts the finger and the game casts in that direction."""

    def __init__(self, touch, pt, aspect):
        self.touch, self.x, self.y = touch, pt["x"], pt["y"]
        self.r = pt.get("r", SKILL_R)
        self.k = pt.get("sens", 1.0) * LOOK_SCALE
        self.aspect = aspect
        c = pt.get("cancel")
        self.cancel_at = tuple(c) if c else None
        self.slot = None
        self.ox = self.oy = 0.0  # finger offset from the button, in height units
        self.cancelled = False

    def key(self, code, down):
        if down and self.slot is None:
            self.ox = self.oy = 0.0
            self.cancelled = False
            self.slot = self.touch.down(self.x, self.y)
        elif not down:
            self.release()

    def motion(self, dx, dy):
        if self.slot is None or self.cancelled or not (dx or dy):
            return
        ox, oy = self.ox + dx * self.k, self.oy + dy * self.k
        n = math.hypot(ox, oy)
        if n > self.r:
            ox, oy = ox / n * self.r, oy / n * self.r
        self.ox, self.oy = ox, oy
        self.touch.move(self.slot, self.x + ox / self.aspect, self.y + oy)

    def cancel(self):
        """Drag onto the game's cancel area; the release then calls the skill off."""
        if self.slot is None or self.cancelled or self.cancel_at is None:
            return
        self.cancelled = True
        self.touch.move(self.slot, *self.cancel_at)

    def release(self):
        if self.slot is not None:
            self.touch.up(self.slot)
            self.slot = None


class Mapper:
    STICK_SPEED = 900.0  # virtual mouse counts/s at full stick deflection

    def __init__(self, touch, profile, aspect=16 / 9):
        self.controls = []
        self.bindings = {}  # keycode -> [(mods frozenset, control index)]
        self.look = None
        self.look_stick = None  # gamepad stick that also turns the camera
        self.stick_controls = {}  # "left"/"right" -> [joystick controls]
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
                    stick = pt.get("stick")
                    self.look_stick = stick if stick in STICK_NAMES else None
            elif pt.get("type") == "joystick":
                c = Joystick(touch, pt, aspect)
                binds = [(code, frozenset()) for code in c.dirs]
                stick = pt.get("stick")
                if stick in STICK_NAMES:
                    self.stick_controls.setdefault(stick, []).append(c)
            else:
                c = Skill(touch, pt, aspect) if pt.get("type") == "skill" else Tap(touch, pt)
                code = keycode(pt.get("key"))
                binds = [] if code is None else [(code, frozenset(pt.get("mods") or ()))]
            self.controls.append(c)
            for code, mods in binds:
                self.bindings.setdefault(code, []).append((mods, i))
        # Sticks the engine must take away from the pad: joystick controls and
        # the camera area, if any.
        self.stick_names = set(self.stick_controls) | ({self.look_stick} if self.look_stick else set())
        # Mouse buttons bound to taps: the mouse must then be captured while active.
        self.mouse_binds = any(e.BTN_MOUSE <= c < e.BTN_JOYSTICK for c in self.bindings)
        # Skill keys aim with the mouse, so they need it captured as well.
        self.skills = any(isinstance(c, Skill) for c in self.controls)
        # The profile uses a gamepad (a button binding or a stick): the engine
        # then grabs pads and mirrors everything the profile does not use.
        self.pad = bool(self.stick_names) or any(is_gamepad_button(c) for c in self.bindings)
        self.aiming = []  # indexes of held skill keys; the latest one gets the mouse
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
                if i in self.aiming:
                    self.aiming.remove(i)
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
        if isinstance(self.controls[i], Skill) and i not in self.aiming:
            self.aiming.append(i)
        return i

    @staticmethod
    def _steer(x, y):
        """Stick vector with the drift around the centre cut out.

        Camera and aim speed keep their full range: just outside the centre
        the camera starts moving slowly instead of jumping to 10 %."""
        mag = math.hypot(x, y)
        if mag <= STICK_DEAD:
            return 0.0, 0.0
        k = (mag - STICK_DEAD) / (1.0 - STICK_DEAD) / mag
        return x * k, y * k

    def sticks(self, left, right, dt, now, look=True):
        """Feed the pads' stick vectors (-1..1) for one frame.

        Sticks drive joystick controls directly. The right stick also stands
        in for the mouse: while a skill key is held it aims that skill,
        otherwise it turns the camera if the look area names a stick."""
        for name, v in (("left", left), ("right", right)):
            for c in self.stick_controls.get(name, ()):
                c.axis(*v)
        rx, ry = self._steer(*right)
        if self.aiming:
            # Like the mouse: while a skill is held the camera stays put.
            if rx or ry:
                self.aim(rx * self.STICK_SPEED * dt, ry * self.STICK_SPEED * dt)
            return
        if not look or self.look is None or not self.look_stick:
            return
        x, y = self._steer(*(left if self.look_stick == "left" else right))
        if x or y:
            self.look.motion(x * self.STICK_SPEED * dt, y * self.STICK_SPEED * dt, now)

    def aim(self, dx, dy):
        """Mouse motion while a skill key is held. Returns False when none is."""
        if not self.aiming:
            return False
        self.controls[self.aiming[-1]].motion(dx, dy)
        return True

    def cancel_aim(self):
        if self.aiming:
            self.controls[self.aiming[-1]].cancel()

    def release_all(self):
        self.fired.clear()
        self.aiming.clear()
        for c in self.controls:
            c.reset() if isinstance(c, Look) else c.release()
