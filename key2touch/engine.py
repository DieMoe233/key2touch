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
        self.kbds = {}  # fd -> InputDevice
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
                  focus=self.focus_app, keyboards=len(self.kbds),
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
            if not is_keyboard(dev):
                dev.close()
                continue
            self.kbds[dev.fd] = dev
            if self.want_grab:
                self.try_grab(dev)
            self.emit_state()
        for path in self.paths - nodes:
            self.paths.discard(path)
            for fd, dev in list(self.kbds.items()):
                if dev.path == path:
                    self.drop(fd)

    def drop(self, fd):
        dev = self.kbds.pop(fd, None)
        self.grabbed_fds.discard(fd)
        # An unplugged keyboard never sends its releases: lift everything it
        # was holding, or keys autorepeat / fingers stay down in the game.
        held = self.pass_down.pop(fd, set())
        for code in held:
            self.passthru.write(e.EV_KEY, code, 0)
        if held:
            self.passthru.syn()
        self.mapper.release_all()
        self.mapper.mods_down.clear()
        self.hotkey_down.clear()
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

    # ---- state ----
    def update_active(self):
        focus_ok = True
        rules = self.profile.get("windows") or []
        if self.focus_src and self.profile.get("only_focused", False) and rules:
            focus_ok = window_matches(self.focus_app, rules)
        active = self.enabled and focus_ok and bool(self.mapper.bindings)
        if active != self.active:
            self.active = active
            if not active:
                self.mapper.release_all()
        self.set_grab(self.active and self.profile.get("grab", True))
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
            if code in self.hotkey_down and val != 1:
                if val == 0:
                    self.hotkey_down.discard(code)
                continue
            action = self.match_hotkey(code) if val == 1 else None
            if action:
                self.hotkey_down.add(code)
                if action == "edit":
                    self.emit(ev="hotkey", action="edit")  # the GUI opens / closes the editor
                    continue
                self.enabled = not self.enabled
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
                fds = [0, *self.kbds]
                if self.focus_src:
                    fds.append(self.focus_src.fileno())
                ready, _, _ = select.select(fds, [], [], 1.0)
                for fd in ready:
                    if fd == 0:
                        self.on_stdin()
                    elif self.focus_src and fd == self.focus_src.fileno():
                        self.on_focus()
                    elif fd in self.kbds:
                        self.on_key_events(self.kbds[fd])
                now = time.monotonic()
                if now - last_scan >= 1.0:
                    last_scan = now
                    self.rescan()
                    if not ready:
                        gc.collect(0)
        finally:
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
