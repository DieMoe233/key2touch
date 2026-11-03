"""Input engine: runs as its own process (own GIL, no GTK) for low latency.

stdin  ← JSON lines from GUI: {"cmd": "profile", "profile": {...}, "aspect": w/h}
                               {"cmd": "enable", "on": bool} | {"cmd": "quit"}
stdout → JSON lines to GUI:   {"ev": "state", ...} | {"ev": "key", "code": n, "down": b}
"""

import fnmatch
import gc
import json
import os
import resource
import select
import subprocess
import sys
import threading
import time

import evdev
from evdev import ecodes as e, UInput

from . import desktop
from .mapper import Mapper, keycode
from .touch import TouchScreen

OWN_PREFIX = "key2touch"


RT_PRIORITY = 10
RT_TIME_US = 100_000  # rtkit requires RLIMIT_RTTIME <= its RTTimeUSecMax (200 ms)


def _rtkit(method, *args):
    return subprocess.run(
        ["gdbus", "call", "--system", "--dest", "org.freedesktop.RealtimeKit1",
         "--object-path", "/org/freedesktop/RealtimeKit1",
         "--method", f"org.freedesktop.RealtimeKit1.{method}", *map(str, args)],
        capture_output=True, text=True, timeout=3).returncode == 0


def raise_priority():
    """Ask rtkit for SCHED_RR so a game saturating the CPU can't delay key
    handling by milliseconds; fall back to a high nice level."""
    pid, tid = os.getpid(), threading.get_native_id()
    try:
        resource.setrlimit(resource.RLIMIT_RTTIME, (RT_TIME_US, RT_TIME_US))
        if _rtkit("MakeThreadRealtimeWithPID", pid, tid, RT_PRIORITY):
            return f"realtime (SCHED_RR {RT_PRIORITY})"
        if _rtkit("MakeThreadHighPriorityWithPID", pid, tid, -15):
            return "nice -15"
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return "normal"


def log(msg):
    """One line per state change in ~/.cache/key2touch/engine.log (stderr)."""
    print(time.strftime("%H:%M:%S"), msg, file=sys.stderr, flush=True)


def window_matches(app_id, rules):
    """Shell-style, case-insensitive: "Waydroid", "waydroid.*" ..."""
    if not app_id:
        return False
    app_id = app_id.lower()
    return any(fnmatch.fnmatchcase(app_id, r.strip().lower()) for r in rules if r.strip())


def is_keyboard(dev):
    if dev.name.startswith(OWN_PREFIX):
        return False
    keys = dev.capabilities().get(e.EV_KEY, [])
    return e.KEY_A in keys and e.KEY_SPACE in keys


def is_mouse(dev):
    if dev.name.startswith(OWN_PREFIX) or is_keyboard(dev):
        return False
    caps = dev.capabilities()
    rel, keys = caps.get(e.EV_REL, []), caps.get(e.EV_KEY, [])
    return e.REL_X in rel and e.REL_Y in rel and e.BTN_LEFT in keys


def grabbable(dev):
    """Combo devices (keyboard + touchpad/mouse on one node, e.g. Logitech
    K400) are read but never grabbed: we only re-inject keys, so grabbing
    them would freeze their pointer."""
    caps = dev.capabilities()
    return e.EV_REL not in caps and e.EV_ABS not in caps


class Engine:
    def __init__(self):
        self.touch = TouchScreen()
        self.passthru = None  # virtual keyboard, created on first grab
        self.pass_down = {}  # fd -> keycodes currently held via the passthrough device
        self.dev_down = {}  # fd -> every keycode currently held on that device
        self.kbds = {}  # fd -> InputDevice
        self.mice = {}  # fd -> InputDevice; grabbed only while camera mode is on
        self.mice_grabbed = set()
        self.want_mouse_grab = False
        self.cursor_free = False  # mouse cursor shown instead of camera mode
        self.cursor_tap = False  # cursor key is down and nothing else was pressed since
        self.paths = set()
        self.want_grab = False
        self.grabbed_fds = set()
        self.profile = {}
        self.mapper = Mapper(self.touch, {})
        self.hotkeys = {}  # keycode -> [(mods frozenset, action)]
        self.hotkey_down = set()  # keycodes whose press fired a hotkey; swallow release/repeat
        self.hotkeys_on = True  # off while the GUI records a new hotkey
        self.enabled = True
        self.backend = desktop.detect()
        self.focus_src = None  # None: this desktop can't report focus
        self.focus_app = None
        self.stdin_buf = b""
        self.active = False
        self.priority = "normal"
        self.out_fd = sys.stdout.fileno()
        os.set_blocking(self.out_fd, False)

    # ---- output to GUI (never blocks the input path) ----
    def emit(self, **msg):
        try:
            os.write(self.out_fd, (json.dumps(msg) + "\n").encode())
        except (BlockingIOError, BrokenPipeError):
            pass

    def emit_state(self):
        self.emit(ev="state", active=self.active, enabled=self.enabled,
                  focus=self.focus_app, keyboards=len(self.kbds), mice=len(self.mice),
                  look=self.want_mouse_grab,
                  priority=self.priority, desktop=self.backend.name,
                  focus_tracking=self.focus_src is not None)

    # ---- devices ----
    def rescan(self):
        try:
            nodes = {f"/dev/input/{n}" for n in os.listdir("/dev/input") if n.startswith("event")}
        except OSError:
            return
        for path in nodes - self.paths:
            self.paths.add(path)
            try:
                dev = evdev.InputDevice(path)
            except OSError:
                continue
            if is_mouse(dev):
                self.mice[dev.fd] = dev
                if self.want_mouse_grab:
                    self.try_grab_mouse(dev)
                continue
            if not is_keyboard(dev):
                dev.close()
                continue
            self.kbds[dev.fd] = dev
            if self.want_grab:
                self.try_grab(dev)
            self.emit_state()
        for path in self.paths - nodes:
            self.paths.discard(path)
            for fd, dev in list(self.kbds.items()) + list(self.mice.items()):
                if dev.path == path:
                    self.drop(fd)

    def drop(self, fd):
        dev = self.kbds.pop(fd, None) or self.mice.pop(fd, None)
        self.grabbed_fds.discard(fd)
        self.mice_grabbed.discard(fd)
        # An unplugged keyboard never sends its releases: lift everything it
        # was holding, or keys autorepeat / fingers stay down in the game.
        held = self.pass_down.pop(fd, set())
        for code in held:
            self.passthru.write(e.EV_KEY, code, 0)
        if held:
            self.passthru.syn()
        # Only what *this* device held: another keyboard (e.g. Sunshine's
        # virtual one) coming and going must not cut the player's touches.
        for code in self.dev_down.pop(fd, set()):
            self.mapper.track(code, 0)
            idx = self.mapper.key(code, 0)
            if idx is not None:
                self.emit(ev="key", code=code, idx=idx, down=False)
            self.hotkey_down.discard(code)
        if dev:
            try:
                dev.close()
            except OSError:
                pass
            self.emit_state()

    def try_grab(self, dev):
        # Only grab while no key is held on this device: a key pressed before
        # the grab would have its release swallowed and get stuck in niri.
        if dev.fd in self.grabbed_fds or not grabbable(dev):
            return
        try:
            if dev.active_keys():
                return
            dev.grab()
            if dev.active_keys():
                # A key went down between the check and the grab; its press
                # reached the compositor, so its release must too. Retry later.
                dev.ungrab()
                return
            self.grabbed_fds.add(dev.fd)
        except OSError:
            pass

    def set_grab(self, on):
        if on == self.want_grab:
            return
        self.want_grab = on
        if on:
            if self.passthru is None:
                self.passthru = UInput(name="key2touch passthrough keyboard")
            for dev in self.kbds.values():
                self.try_grab(dev)
            return
        for fd in self.grabbed_fds:
            try:
                self.kbds[fd].ungrab()
            except (OSError, KeyError):
                pass
        self.grabbed_fds.clear()
        # Keys pressed through the passthrough device would otherwise stay
        # stuck: their real release now goes to libinput, which never saw
        # the press.
        held = set().union(*self.pass_down.values()) if self.pass_down else set()
        for code in held:
            self.passthru.write(e.EV_KEY, code, 0)
        if held:
            self.passthru.syn()
        self.pass_down.clear()

    # ---- mice / camera mode ----
    def try_grab_mouse(self, dev):
        # Same rule as keyboards: never grab with a button held, or its
        # release is swallowed and the button sticks in the compositor.
        if dev.fd in self.mice_grabbed:
            return
        try:
            if dev.active_keys():
                return
            dev.grab()
            if dev.active_keys():
                dev.ungrab()
                return
            self.mice_grabbed.add(dev.fd)
        except OSError:
            pass

    def update_look(self):
        """Camera mode = mapping active + a look point + cursor key not held.
        While on, mice are grabbed: motion drives the camera, buttons can be
        bound like keys, the cursor stays put."""
        want = bool(self.active and self.mapper.look and not self.cursor_free)
        if want == self.want_mouse_grab:
            return
        self.want_mouse_grab = want
        if want:
            for dev in self.mice.values():
                self.try_grab_mouse(dev)
        else:
            self.mapper.look and self.mapper.look.reset()
            for code in [c for c in self.mapper.fired if e.BTN_MISC <= c < e.KEY_OK]:
                idx = self.mapper.key(code, 0)  # mouse-button taps
                if idx is not None:
                    self.emit(ev="key", code=code, idx=idx, down=False)
            for fd in self.mice_grabbed:
                try:
                    self.mice[fd].ungrab()
                except (OSError, KeyError):
                    pass
            self.mice_grabbed.clear()
        self.emit_state()

    def on_mouse_events(self, dev):
        try:
            events = list(dev.read())
        except BlockingIOError:
            return
        except OSError:
            self.drop(dev.fd)
            return
        if dev.fd not in self.mice_grabbed:
            # Not ours right now: the compositor gets these events anyway.
            if self.want_mouse_grab:
                self.try_grab_mouse(dev)
            return
        dx = dy = 0
        for ev in events:
            if ev.type == e.EV_REL:
                if ev.code == e.REL_X:
                    dx += ev.value
                elif ev.code == e.REL_Y:
                    dy += ev.value
            elif ev.type == e.EV_KEY and ev.value != 2:
                self.cursor_tap = False
                if ev.value == 1:
                    self.dev_down.setdefault(dev.fd, set()).add(ev.code)
                else:
                    self.dev_down.get(dev.fd, set()).discard(ev.code)
                idx = self.mapper.key(ev.code, ev.value)  # unbound buttons are dropped
                if idx is not None:
                    self.emit(ev="key", code=ev.code, idx=idx, down=ev.value == 1)
        # One touch move per read batch, however fast the mouse polls.
        if self.mapper.look:
            self.mapper.look.motion(dx, dy, time.monotonic())

    # ---- state ----
    def update_active(self):
        focus_ok = True
        rules = self.profile.get("windows") or []
        if self.focus_src and self.profile.get("only_focused", False) and rules:
            focus_ok = window_matches(self.focus_app, rules)
        active = self.enabled and focus_ok and bool(self.mapper.bindings or self.mapper.look)
        if active != self.active:
            self.active = active
            if not active:
                self.mapper.release_all()
            self.cursor_free = False  # (re)entering the game starts in camera mode
        self.set_grab(self.active and self.profile.get("grab", True))
        self.update_look()
        self.emit_state()

    def set_profile(self, prof, aspect):
        self.mapper.release_all()
        self.profile = prof
        mods = self.mapper.mods_down
        self.mapper = Mapper(self.touch, prof, aspect)
        self.mapper.mods_down = mods
        self.hotkeys = {}
        for action, key, mods in (("toggle", "toggle_key", "toggle_mods"), ("edit", "edit_key", "edit_mods")):
            code = keycode(prof.get(key))
            if code is not None:
                self.hotkeys.setdefault(code, []).append((frozenset(prof.get(mods) or ()), action))
        self.update_active()

    def match_hotkey(self, code):
        """Most specific hotkey whose modifiers are held: with toggle = F12 and
        edit = Shift+F12, Shift+F12 fires only edit."""
        cands = self.hotkeys.get(code) if self.hotkeys_on else None
        if not cands:
            return None
        held = self.mapper.mod_groups(exclude=code)
        best = max((c for c in cands if c[0] <= held), key=lambda c: len(c[0]), default=None)
        return best[1] if best else None

    # ---- event handling (hot path) ----
    def on_key_events(self, dev):
        try:
            # read() is a lazy generator: materialize it here so an unplugged
            # device (ENODEV) is caught instead of killing the engine.
            events = list(dev.read())
        except BlockingIOError:
            return
        except OSError:
            self.drop(dev.fd)
            return
        grabbed = dev.fd in self.grabbed_fds
        pass_dirty = False
        for ev in events:
            if ev.type != e.EV_KEY:
                continue
            code, val = ev.code, ev.value
            self.mapper.track(code, val)
            if val == 1:
                self.dev_down.setdefault(dev.fd, set()).add(code)
            elif val == 0:
                self.dev_down.get(dev.fd, set()).discard(code)
            if code in self.hotkey_down and val != 1:
                if val == 0:
                    self.hotkey_down.discard(code)
                continue
            if code == self.mapper.cursor_key and val != 2:
                # Not swallowed: the key (default Ctrl) also serves in combos.
                if self.mapper.cursor_mode == "hold":
                    self.cursor_free = val == 1
                    self.update_look()
                elif val == 1:
                    self.cursor_tap = True
                elif self.cursor_tap:  # pressed and released on its own: toggle
                    self.cursor_tap = False
                    self.cursor_free = not self.cursor_free
                    self.update_look()
            elif val == 1:
                self.cursor_tap = False  # Ctrl+Q etc. is a combo, not a tap
            action = self.match_hotkey(code) if val == 1 else None
            if action:
                self.hotkey_down.add(code)
                if action == "edit":
                    self.emit(ev="hotkey", action="edit")  # the GUI opens / closes the editor
                    continue
                self.enabled = not self.enabled
                log(f"mapping {'on' if self.enabled else 'off'}: hotkey {e.KEY.get(code)} on {dev.name!r}")
                self.emit(ev="toggled", enabled=self.enabled)  # the GUI notifies the user
                if pass_dirty:
                    self.passthru.syn()
                    pass_dirty = False
                self.update_active()
                # The grab may have just ended (or begun): don't keep
                # routing the rest of this batch through passthrough.
                grabbed = dev.fd in self.grabbed_fds
                continue
            if self.active or val != 1:
                idx = self.mapper.key(code, val)
                if idx is not None:
                    if val != 2:
                        self.emit(ev="key", code=code, idx=idx, down=val == 1)
                    continue
            if grabbed:
                self.passthru.write(e.EV_KEY, code, val)
                pass_dirty = True
                if val == 1:
                    self.pass_down.setdefault(dev.fd, set()).add(code)
                elif val == 0:
                    self.pass_down.get(dev.fd, set()).discard(code)
        if pass_dirty:
            self.passthru.syn()
        if self.want_grab and not grabbed:
            self.try_grab(dev)

    def on_focus(self):
        try:
            changes = self.focus_src.read()
        except (OSError, EOFError, ValueError) as ex:
            print(f"focus tracking lost: {ex}", file=sys.stderr)
            self.focus_src = None
            self.update_active()
            return
        if changes and changes[-1] != self.focus_app:
            self.focus_app = changes[-1]
            self.update_active()

    def on_stdin(self):
        data = os.read(0, 65536)
        if not data:
            raise SystemExit(0)
        self.stdin_buf += data
        while b"\n" in self.stdin_buf:
            line, self.stdin_buf = self.stdin_buf.split(b"\n", 1)
            msg = json.loads(line)
            cmd = msg.get("cmd")
            if cmd == "profile":
                self.set_profile(msg["profile"], msg.get("aspect", 16 / 9))
            elif cmd == "hotkeys":
                self.hotkeys_on = bool(msg["on"])
            elif cmd == "enable":
                if bool(msg["on"]) != self.enabled:
                    log(f"mapping {'on' if msg['on'] else 'off'}: from GUI ({msg.get('why', 'switch')})")
                self.enabled = bool(msg["on"])
                self.update_active()
            elif cmd == "quit":
                raise SystemExit(0)

    def run(self):
        try:
            self.focus_src = self.backend.focus_source()
            self.focus_app = getattr(self.focus_src, "initial_app", None)
        except (OSError, EOFError, ValueError, KeyError) as ex:
            print(f"{self.backend.name}: no focus tracking ({ex})", file=sys.stderr)
            self.focus_src = None
        self.priority = raise_priority()
        self.rescan()
        # The hot path allocates little; a GC pass is the only thing that could
        # stall it, so freeze startup objects and collect only when idle.
        gc.collect()
        gc.freeze()
        gc.disable()
        self.emit_state()
        last_scan = time.monotonic()
        try:
            while True:
                fds = [0, *self.kbds, *self.mice]
                if self.focus_src:
                    fds.append(self.focus_src.fileno())
                timeout = 1.0
                look = self.mapper.look
                if look and look.deadline() is not None:
                    timeout = max(0.0, min(timeout, look.deadline() - time.monotonic()))
                ready, _, _ = select.select(fds, [], [], timeout)
                for fd in ready:
                    if fd == 0:
                        self.on_stdin()
                    elif self.focus_src and fd == self.focus_src.fileno():
                        self.on_focus()
                    elif fd in self.kbds:
                        self.on_key_events(self.kbds[fd])
                    elif fd in self.mice:
                        self.on_mouse_events(self.mice[fd])
                if self.mapper.look:
                    self.mapper.look.idle(time.monotonic())
                now = time.monotonic()
                if now - last_scan >= 1.0:
                    last_scan = now
                    self.rescan()
                    if not ready:
                        gc.collect(0)
        finally:
            self.active = False
            self.update_look()  # ungrabs the mice
            self.mapper.release_all()
            self.set_grab(False)
            self.touch.close()


def main():
    try:
        Engine().run()
    except SystemExit:
        pass
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
