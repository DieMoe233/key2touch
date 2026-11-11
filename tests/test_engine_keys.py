"""Keyboard routing in the engine: mapped keys become touches, unbound keys
are passed through while the keyboard is grabbed (the pad path is in
test_engine_pad)."""
import unittest
from types import SimpleNamespace as NS

from evdev import ecodes as e

from key2touch.engine import Engine
from key2touch.mapper import Mapper
from key2touch.profile import new_tap
from tests.test_mapper import FakeTouch


class Recorder:
    def __init__(self):
        self.log = []

    def write(self, ev_type, code, value):
        self.log.append((ev_type, code, value))

    def syn(self):
        pass

    def key(self, code, value):
        pass


class FakeKeyboard:
    fd = 5
    name = "Fake Keyboard"

    def __init__(self):
        self.batch = []

    def read(self):
        batch, self.batch = self.batch, []
        return iter(batch)

    def close(self):
        pass


def key(code, value):
    return NS(type=e.EV_KEY, code=code, value=value)


class KeyboardRoutingTests(unittest.TestCase):
    def setUp(self):
        eng = Engine.__new__(Engine)  # skip __init__: it opens uinput devices
        self.touch = FakeTouch()
        eng.touch = self.touch
        eng.mapper = Mapper(self.touch, {"points": [new_tap(0.5, 0.5, "KEY_J")]})
        eng.active, eng.enabled = True, True
        eng.kbds, eng.mice, eng.pads = {}, {}, {}
        eng.grabbed_fds, eng.mice_grabbed = {FakeKeyboard.fd}, set()
        eng.pass_down = {}  # fd -> keys held through the passthrough
        eng.dev_down = {}
        eng.hotkey_down, eng.hotkeys, eng.hotkeys_on = set(), {}, True
        eng.passthru, eng.mirror = Recorder(), Recorder()
        eng.want_grab = True
        eng.profile = {}
        eng.cursor_tap = False
        eng.cursor_free = False
        eng.msgs = []
        eng.emit = lambda **kw: eng.msgs.append(kw)
        eng.emit_state = lambda: None
        eng.try_grab = lambda dev: None
        self.eng = eng
        self.dev = FakeKeyboard()
        eng.kbds[self.dev.fd] = self.dev

    def feed(self, *events):
        self.dev.batch = list(events)
        self.eng.on_key_events(self.dev)

    def test_unbound_key_goes_through_the_passthrough(self):
        self.feed(key(e.KEY_K, 1))
        self.assertEqual(self.eng.passthru.log, [(e.EV_KEY, e.KEY_K, 1)])
        self.assertEqual(self.eng.pass_down[self.dev.fd], {e.KEY_K})
        self.feed(key(e.KEY_K, 0))
        self.assertEqual(self.eng.pass_down[self.dev.fd], set())
        self.assertEqual(self.eng.passthru.log[-1], (e.EV_KEY, e.KEY_K, 0))

    def test_mapped_key_becomes_a_touch_and_is_swallowed(self):
        self.feed(key(e.KEY_J, 1), key(e.KEY_J, 0))
        self.assertEqual(self.touch.log, [("down", 0, 0.5, 0.5), ("up", 0)])
        self.assertEqual(self.eng.passthru.log, [])

    def test_ungrabbed_key_is_left_to_the_compositor(self):
        self.eng.grabbed_fds.clear()
        self.feed(key(e.KEY_K, 1))
        self.assertEqual(self.eng.passthru.log, [])
        self.assertEqual(self.eng.pass_down.get(self.dev.fd, set()), set())


if __name__ == "__main__":
    unittest.main()
