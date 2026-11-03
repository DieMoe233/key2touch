import unittest

from evdev import ecodes as e

from key2touch.mapper import Mapper
from key2touch.profile import new_joystick, new_tap


class FakeTouch:
    def __init__(self):
        self.log = []
        self.next = 0
        self.live = set()

    def down(self, x, y):
        s = self.next
        self.next += 1
        self.live.add(s)
        self.log.append(("down", s, round(x, 4), round(y, 4)))
        return s

    def move(self, s, x, y):
        self.log.append(("move", s, round(x, 4), round(y, 4)))

    def up(self, s):
        self.live.discard(s)
        self.log.append(("up", s))


def prof(*pts):
    return {"points": list(pts)}


class TapTests(unittest.TestCase):
    def test_press_release(self):
        t = FakeTouch()
        m = Mapper(t, prof(new_tap(0.5, 0.25, "KEY_J")))
        self.assertEqual(m.key(e.KEY_J, 1), 0)
        self.assertEqual(m.key(e.KEY_J, 2), 0)  # autorepeat consumed, no event
        self.assertEqual(m.key(e.KEY_J, 0), 0)
        self.assertEqual(t.log, [("down", 0, 0.5, 0.25), ("up", 0)])

    def test_unmapped_passes(self):
        m = Mapper(FakeTouch(), prof(new_tap(0.5, 0.5, "KEY_J")))
        self.assertIsNone(m.key(e.KEY_K, 1))

    def test_multitouch_independent(self):
        t = FakeTouch()
        m = Mapper(t, prof(new_tap(0.1, 0.1, "KEY_J"), new_tap(0.9, 0.9, "KEY_K")))
        m.key(e.KEY_J, 1)
        m.key(e.KEY_K, 1)
        self.assertEqual(t.live, {0, 1})
        m.key(e.KEY_J, 0)
        self.assertEqual(t.live, {1})

    def test_release_all(self):
        t = FakeTouch()
        m = Mapper(t, prof(new_tap(0.1, 0.1, "KEY_J")))
        m.key(e.KEY_J, 1)
        m.release_all()
        self.assertEqual(t.live, set())
        m.key(e.KEY_J, 0)  # stray release after reset is harmless
        self.assertEqual(t.log[-1], ("up", 0))


def press(m, code, v):
    m.track(code, v)
    return m.key(code, v)


class ComboTests(unittest.TestCase):
    def setUp(self):
        self.t = FakeTouch()
        ctrl_q = dict(new_tap(0.9, 0.9, "KEY_Q"), mods=["CTRL"])
        self.m = Mapper(self.t, prof(new_tap(0.1, 0.1, "KEY_Q"), ctrl_q, new_tap(0.5, 0.5, "KEY_LEFTSHIFT")))

    def test_plain_and_combo(self):
        self.assertEqual(press(self.m, e.KEY_Q, 1), 0)
        press(self.m, e.KEY_Q, 0)
        press(self.m, e.KEY_RIGHTCTRL, 1)  # either side counts
        self.assertEqual(press(self.m, e.KEY_Q, 1), 1)
        self.assertEqual(self.t.log[-1], ("down", 1, 0.9, 0.9))

    def test_release_after_modifier_lifted(self):
        press(self.m, e.KEY_LEFTCTRL, 1)
        press(self.m, e.KEY_Q, 1)
        press(self.m, e.KEY_LEFTCTRL, 0)
        self.assertEqual(press(self.m, e.KEY_Q, 0), 1)
        self.assertEqual(self.t.live, set())

    def test_combo_only_passes_plain(self):
        m = Mapper(FakeTouch(), prof(dict(new_tap(0.9, 0.9, "KEY_Q"), mods=["CTRL"])))
        self.assertIsNone(press(m, e.KEY_Q, 1))

    def test_modifier_alone(self):
        self.assertEqual(press(self.m, e.KEY_LEFTSHIFT, 1), 2)
        self.assertEqual(press(self.m, e.KEY_LEFTSHIFT, 0), 2)


class JoystickTests(unittest.TestCase):
    def setUp(self):
        self.t = FakeTouch()
        js = new_joystick(0.2, 0.7, r=0.1)
        self.m = Mapper(self.t, prof(js), aspect=2.0)

    def test_down_at_centre_then_move(self):
        self.m.key(e.KEY_W, 1)
        self.assertEqual(self.t.log, [("down", 0, 0.2, 0.7), ("move", 0, 0.2, 0.6)])

    def test_aspect_correction(self):
        self.m.key(e.KEY_D, 1)
        # radius 0.1 of height, aspect 2 -> 0.05 of width
        self.assertEqual(self.t.log[-1], ("move", 0, 0.25, 0.7))

    def test_diagonal_on_circle(self):
        self.m.key(e.KEY_W, 1)
        self.m.key(e.KEY_D, 1)
        _, _, x, y = self.t.log[-1]
        self.assertAlmostEqual(((x - 0.2) / 0.05) ** 2 + ((y - 0.7) / 0.1) ** 2, 1, places=2)

    def test_single_contact_and_release(self):
        self.m.key(e.KEY_W, 1)
        self.m.key(e.KEY_A, 1)
        self.m.key(e.KEY_W, 0)
        self.assertEqual(self.t.log[-1], ("move", 0, 0.15, 0.7))
        self.m.key(e.KEY_A, 0)
        self.assertEqual(self.t.log[-1], ("up", 0))
        self.assertEqual(sum(1 for l in self.t.log if l[0] == "down"), 1)

    def test_opposites_cancel_to_centre(self):
        self.m.key(e.KEY_A, 1)
        self.m.key(e.KEY_D, 1)
        self.assertEqual(self.t.log[-1], ("move", 0, 0.2, 0.7))


class HotkeyTests(unittest.TestCase):
    def test_most_specific_wins(self):
        from key2touch.engine import Engine
        en = Engine.__new__(Engine)  # only the hotkey matcher; no devices
        en.hotkeys_on = True
        en.mapper = Mapper(FakeTouch(), prof())
        en.hotkeys = {e.KEY_F12: [(frozenset(), "toggle"), (frozenset({"SHIFT"}), "edit")]}
        self.assertEqual(en.match_hotkey(e.KEY_F12), "toggle")
        en.mapper.track(e.KEY_LEFTSHIFT, 1)
        self.assertEqual(en.match_hotkey(e.KEY_F12), "edit")
        en.hotkeys_on = False
        self.assertIsNone(en.match_hotkey(e.KEY_F12))


class WindowRuleTests(unittest.TestCase):
    def test_rules(self):
        from key2touch.engine import window_matches
        rules = ["Waydroid", "waydroid.*", " "]
        self.assertTrue(window_matches("Waydroid", rules))
        self.assertTrue(window_matches("waydroid.com.miHoYo.GenshinImpact", rules))
        self.assertFalse(window_matches("firefox", rules))
        self.assertFalse(window_matches(None, rules))


if __name__ == "__main__":
    unittest.main()


class LookTests(unittest.TestCase):
    def setUp(self):
        from key2touch.profile import new_look
        self.t = FakeTouch()
        pt = new_look(0.6, 0.4, r=0.1)
        pt["sens"] = 2.0  # 0.001 of the height per count
        self.m = Mapper(self.t, {"points": [pt]}, aspect=2.0)
        self.look = self.m.look

    def test_drag_from_centre(self):
        self.look.motion(20, -10, now=1.0)
        self.assertEqual(self.t.log[0], ("down", 0, 0.6, 0.4))
        # 20 counts * 0.001 = 0.02 height = 0.01 width at aspect 2
        self.assertEqual(self.t.log[1], ("move", 0, 0.61, 0.39))

    def test_recentre_at_edge(self):
        self.look.motion(90, 0, now=1.0)
        self.look.motion(30, 0, now=1.01)  # would pass r=0.1: lift, start again at the centre
        self.assertEqual([l[0] for l in self.t.log], ["down", "move", "up", "down", "move"])
        self.assertEqual(self.t.log[-1], ("move", 1, 0.615, 0.4))

    def test_idle_lift(self):
        self.look.motion(5, 5, now=1.0)
        self.look.idle(now=1.1)
        self.assertEqual(self.t.live, {0})
        self.look.idle(now=1.5)
        self.assertEqual(self.t.live, set())

    def test_mouse_button_binding(self):
        from key2touch.profile import new_tap
        m = Mapper(self.t, {"points": [new_tap(0.8, 0.8, "BTN_LEFT")]})
        self.assertEqual(m.key(e.BTN_LEFT, 1), 0)
        self.assertEqual(m.key(e.BTN_LEFT, 0), 0)
