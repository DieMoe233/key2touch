"""Mouse routing in the engine while a skill key is held (no real devices)."""
import unittest
from types import SimpleNamespace as NS

from evdev import ecodes as e

from key2touch.engine import Engine
from key2touch.mapper import Mapper
from key2touch.profile import new_skill, new_tap
from tests.test_mapper import FakeTouch


class Recorder:
    def __init__(self):
        self.log = []

    def write(self, t, c, v):
        self.log.append((t, c, v))

    def syn(self):
        pass

    def key(self, c, v):
        pass


class FakeMouse:
    fd = 7

    def __init__(self):
        self.batch = []

    def read(self):
        b, self.batch = self.batch, []
        return iter(b)


def rel(code, v):
    return NS(type=e.EV_REL, code=code, value=v)


def btn(code, v):
    return NS(type=e.EV_KEY, code=code, value=v)


class AimRoutingTests(unittest.TestCase):
    def setUp(self):
        self.touch = FakeTouch()
        eng = Engine.__new__(Engine)  # skip __init__: it opens uinput devices
        pts = [{**new_skill(0.8, 0.8, "KEY_Q"), "cancel": [0.9, 0.2]}, new_tap(0.5, 0.5, "BTN_LEFT")]
        eng.mapper = Mapper(self.touch, {"points": pts}, aspect=1.0)
        eng.passmouse, eng.mirror = Recorder(), Recorder()
        eng.mice_grabbed, eng.want_mouse_grab = {FakeMouse.fd}, True
        eng.mouse_mode_now = "buttons"
        eng.cursor_tap = False
        eng.dev_down, eng.aim_buttons, eng.passmouse_down = {}, set(), set()
        eng.emit = lambda **kw: None
        self.eng, self.dev = eng, FakeMouse()

    def feed(self, *events):
        self.dev.batch = list(events)
        self.eng.on_mouse_events(self.dev)

    def test_motion_is_the_cursor_until_the_skill_is_held(self):
        self.feed(rel(e.REL_X, 5))
        self.assertEqual(self.eng.passmouse.log, [(e.EV_REL, e.REL_X, 5)])
        self.eng.mapper.key(e.KEY_Q, 1)
        self.feed(rel(e.REL_X, 100), rel(e.REL_Y, 0))
        self.assertEqual(len(self.eng.passmouse.log), 1)  # cursor frozen while aiming
        self.assertEqual(self.touch.log[-1][0], "move")
        self.eng.mapper.key(e.KEY_Q, 0)
        self.feed(rel(e.REL_X, 5))
        self.assertEqual(self.eng.passmouse.log[-1], (e.EV_REL, e.REL_X, 5))

    def test_right_click_cancels_and_its_release_is_swallowed(self):
        self.eng.mapper.key(e.KEY_Q, 1)
        self.feed(btn(e.BTN_RIGHT, 1))
        self.assertEqual(self.touch.log[-1], ("move", 0, 0.9, 0.2))
        self.eng.mapper.key(e.KEY_Q, 0)  # skill released before the button
        self.feed(btn(e.BTN_RIGHT, 0))
        self.assertEqual(self.eng.passmouse.log, [])  # neither press nor release leaked

    def test_unbound_click_while_aiming_is_swallowed(self):
        self.eng.mapper.key(e.KEY_Q, 1)
        self.feed(btn(e.BTN_MIDDLE, 1))
        self.eng.mapper.key(e.KEY_Q, 0)
        self.feed(btn(e.BTN_MIDDLE, 0))
        self.assertEqual(self.eng.passmouse.log, [])

    def test_button_held_before_aiming_still_gets_its_release(self):
        self.feed(btn(e.BTN_MIDDLE, 1))
        self.eng.mapper.key(e.KEY_Q, 1)
        self.feed(btn(e.BTN_MIDDLE, 0))
        self.assertEqual(self.eng.passmouse.log, [(e.EV_KEY, e.BTN_MIDDLE, 1), (e.EV_KEY, e.BTN_MIDDLE, 0)])

    def test_bound_buttons_keep_working_while_aiming(self):
        self.eng.mapper.key(e.KEY_Q, 1)
        self.feed(btn(e.BTN_LEFT, 1))
        self.assertEqual(self.touch.live, {0, 1})  # skill finger + attack tap

    def test_skills_capture_the_mouse(self):
        self.eng.active, self.eng.cursor_free = True, False
        self.assertEqual(self.eng.mouse_mode(), "buttons")


if __name__ == "__main__":
    unittest.main()
