import unittest

from evdev import ecodes as e

from key2touch.mapper import LOOK_SCALE, Mapper
from key2touch.profile import new_joystick, new_skill, new_tap


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

class SkillTests(unittest.TestCase):
    """Drag-to-aim skill keys. aspect 1 keeps the arithmetic readable."""

    def mapper(self, **kw):
        pt = new_skill(0.8, 0.8, "KEY_Q", r=0.1)
        pt.update(kw)
        self.t = FakeTouch()
        return Mapper(self.t, prof(pt, new_tap(0.5, 0.5, "KEY_J")), aspect=1.0)

    def test_quick_cast_is_a_tap(self):
        m = self.mapper()
        m.key(e.KEY_Q, 1)
        m.key(e.KEY_Q, 0)
        self.assertEqual(self.t.log, [("down", 0, 0.8, 0.8), ("up", 0)])

    def test_mouse_drags_finger_while_held(self):
        m = self.mapper()
        self.assertFalse(m.aim(10, 0))  # not held: the mouse is the cursor's
        m.key(e.KEY_Q, 1)
        self.assertTrue(m.aim(100, 0))
        m.key(e.KEY_Q, 0)
        self.assertEqual(self.t.log, [("down", 0, 0.8, 0.8), ("move", 0, round(0.8 + 100 * LOOK_SCALE, 4), 0.8), ("up", 0)])
        self.assertFalse(m.aim(10, 0))

    def test_finger_stays_within_range_and_reverses_at_once(self):
        m = self.mapper()
        m.key(e.KEY_Q, 1)
        m.aim(10000, 0)  # far past the edge: clamped to r
        self.assertEqual(self.t.log[-1], ("move", 0, 0.9, 0.8))
        m.aim(-100, 0)  # coming back reacts immediately, no dead zone
        self.assertEqual(self.t.log[-1], ("move", 0, round(0.9 - 100 * LOOK_SCALE, 4), 0.8))

    def test_cancel_moves_to_cancel_area_and_ignores_motion(self):
        m = self.mapper(cancel=[0.7, 0.2])
        m.key(e.KEY_Q, 1)
        m.aim(50, 50)
        m.cancel_aim()
        m.aim(50, 50)
        m.key(e.KEY_Q, 0)
        self.assertEqual(self.t.log[-2:], [("move", 0, 0.7, 0.2), ("up", 0)])
        self.assertEqual(sum(1 for x in self.t.log if x[0] == "move"), 2)

    def test_cancel_without_area_does_nothing(self):
        m = self.mapper()
        m.key(e.KEY_Q, 1)
        m.cancel_aim()
        m.key(e.KEY_Q, 0)
        self.assertEqual(self.t.log, [("down", 0, 0.8, 0.8), ("up", 0)])

    def test_next_cast_starts_fresh(self):
        m = self.mapper(cancel=[0.7, 0.2])
        m.key(e.KEY_Q, 1)
        m.aim(100, 0)
        m.cancel_aim()
        m.key(e.KEY_Q, 0)
        m.key(e.KEY_Q, 1)
        m.aim(0, 100)  # aims again, from the button centre
        self.assertEqual(self.t.log[-1], ("move", 1, 0.8, round(0.8 + 100 * LOOK_SCALE, 4)))

    def test_latest_held_skill_gets_the_mouse(self):
        t = FakeTouch()
        m = Mapper(t, prof(new_skill(0.6, 0.8, "KEY_Q"), new_skill(0.8, 0.8, "KEY_E")), aspect=1.0)
        m.key(e.KEY_Q, 1)
        m.key(e.KEY_E, 1)
        m.aim(100, 0)
        self.assertEqual(t.log[-1][:2], ("move", 1))
        m.key(e.KEY_E, 0)
        m.aim(100, 0)
        self.assertEqual(t.log[-1][:2], ("move", 0))

    def test_needs_the_mouse_and_release_all(self):
        m = self.mapper()
        self.assertTrue(m.skills)
        m.key(e.KEY_Q, 1)
        m.release_all()
        self.assertEqual(self.t.live, set())
        self.assertFalse(m.aim(10, 0))
        self.assertFalse(Mapper(FakeTouch(), prof(new_tap(0.5, 0.5, "KEY_J"))).skills)


if __name__ == "__main__":
    unittest.main()


class LookTests(unittest.TestCase):
    def setUp(self):
        from key2touch.profile import new_look
        self.t = FakeTouch()
        pt = new_look(0.6, 0.4, r=0.1)  # below LOOK_MIN_R: the mapper clamps it
        pt["sens"] = 2.0  # 0.001 of the height per count
        self.m = Mapper(self.t, {"points": [pt]}, aspect=2.0)
        self.look = self.m.look
        self.look.r = 0.1  # exercise the geometry with a round number

    def test_drag_from_centre(self):
        self.look.motion(20, -10, now=1.0)
        self.assertEqual(self.t.log[0], ("down", 0, 0.6, 0.4))
        # 20 counts * 0.001 = 0.02 height = 0.01 width at aspect 2
        self.assertEqual(self.t.log[1], ("move", 0, 0.61, 0.39))

    def test_restart_at_edge(self):
        self.look.motion(90, 0, now=1.0)  # 0.09 of r=0.1
        self.look.motion(30, 0, now=1.01)  # crosses the edge after 10 of the 30 counts
        self.assertEqual([l[0] for l in self.t.log], ["down", "move", "move", "up"])
        self.assertAlmostEqual(self.t.log[2][2], 0.6 + 0.1 / 2, places=3)  # exactly at the edge
        self.look.motion(10, 0, now=1.02)  # buffered during the gap
        self.assertEqual(len(self.t.log), 4)
        self.assertEqual(self.look.deadline(), 1.01 + self.look.GAP)
        self.look.idle(now=1.05)  # gap over: new stroke behind the centre, carrying 20 + 10 counts
        self.assertEqual(self.t.log[4][0], "down")
        self.assertAlmostEqual(self.t.log[4][2], 0.6 - 0.07 / 2, places=3)
        self.assertAlmostEqual(self.t.log[5][2], 0.6 + (-0.07 + 0.03) / 2, places=3)

    def test_no_motion_lost(self):
        # A long sweep through a small area: total finger travel == mouse travel.
        for i in range(200):
            self.look.motion(10, 0, now=1.0 + i * 0.01)
            self.look.idle(now=1.0 + i * 0.01)
        self.look.idle(now=10)
        xs, travel, prev = [], 0.0, None
        for entry in self.t.log:
            if entry[0] == "down":
                prev = entry[2]
            elif entry[0] == "move":
                travel += entry[2] - prev
                prev = entry[2]
        self.assertAlmostEqual(travel * 2, 200 * 10 * 0.001, places=2)  # back to height units

    def test_min_size(self):
        from key2touch.profile import LOOK_MIN_R, new_look
        self.assertEqual(Mapper(FakeTouch(), {"points": [new_look(0.5, 0.5, r=0.05)]}).look.r, LOOK_MIN_R)

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

class AnalogStickTests(unittest.TestCase):
    """A gamepad stick on a touch joystick. aspect 2: rx is half of ry."""

    def setUp(self):
        self.t = FakeTouch()
        js = new_joystick(0.2, 0.7, r=0.1)  # WASD as well, plus the left stick
        js["stick"] = "left"
        self.m = Mapper(self.t, prof(js), aspect=2.0)
        self.ctrl = self.m.controls[0]

    def test_deadzone_keeps_the_finger_up(self):
        self.ctrl.axis(0.1, 0.05)  # default deadzone 0.15
        self.assertEqual(self.t.log, [])
        self.assertIsNone(self.ctrl.slot)

    def test_magnitude_drives_distance(self):
        self.ctrl.axis(0.5, 0.0)
        # (0.5 - 0.15) / (1 - 0.15) = 0.41 of the radius, from the centre first
        self.assertEqual(self.t.log[0], ("down", 0, 0.2, 0.7))
        self.assertEqual(self.t.log[1], ("move", 0, 0.2206, 0.7))

    def test_full_deflection_reaches_the_rim(self):
        self.ctrl.axis(0.0, -1.0)
        self.assertEqual(self.t.log[-1], ("move", 0, 0.2, 0.6))
        self.ctrl.axis(1.0, 0.0)
        self.assertEqual(self.t.log[-1], ("move", 0, 0.25, 0.7))

    def test_returning_to_the_centre_lifts_the_finger(self):
        self.ctrl.axis(0.9, 0.0)
        self.ctrl.axis(0.0, 0.0)
        self.assertEqual(self.t.log[-1], ("up", 0))
        self.assertEqual(self.t.live, set())

    def test_keys_and_stick_add_up_within_the_rim(self):
        self.m.key(e.KEY_W, 1)
        self.ctrl.axis(1.0, 0.0)
        # (1, -1) normalised to the circle, then rx/ry applied
        self.assertEqual(self.t.log[-1], ("move", 0, round(0.2 + 0.7071 * 0.05, 4),
                                          round(0.7 - 0.7071 * 0.1, 4)))

    def test_custom_deadzone(self):
        js = new_joystick(0.2, 0.7, stick="left")
        js["deadzone"] = 0.4
        ctrl = Mapper(FakeTouch(), prof(js), aspect=1.0).controls[0]
        ctrl.axis(0.35, 0.0)
        self.assertIsNone(ctrl.slot)


class StickRoutingTests(unittest.TestCase):
    """Left stick -> touch joystick, right stick -> camera / skill aim."""

    def setUp(self):
        from key2touch.profile import new_look
        self.t = FakeTouch()
        js = new_joystick(0.2, 0.7, r=0.1)
        js["stick"] = "left"
        self.m = Mapper(self.t, prof(js, new_look(0.6, 0.4, r=0.1, stick="right")), aspect=1.0)
        self.m.look.r = 0.1

    def test_right_stick_turns_the_camera(self):
        self.m.sticks((0.0, 0.0), (0.5, 0.0), 0.01, now=1.0)
        # 0.5 through the drift deadzone: 0.444 of 900 counts/s * 10 ms = 4 counts
        self.assertEqual([l[0] for l in self.t.log], ["down", "move"])
        self.assertEqual(self.t.log[0][1], self.m.look.slot)
        self.assertEqual(self.t.log[1][2], 0.602)

    def test_stick_drift_does_not_move_the_camera(self):
        self.m.sticks((0.0, 0.0), (0.05, 0.02), 0.01, now=1.0)
        self.assertEqual(self.t.log, [])

    def test_camera_off_while_the_mouse_is_free(self):
        self.m.sticks((0.0, 0.0), (1.0, 0.0), 0.01, now=1.0, look=False)
        self.assertEqual(self.t.log, [])

    def test_right_stick_aims_a_held_skill(self):
        from key2touch.profile import new_look
        t = FakeTouch()
        m = Mapper(t, prof(new_skill(0.8, 0.8, "KEY_Q"),
                           new_look(0.6, 0.4, stick="right")), aspect=1.0)
        m.key(e.KEY_Q, 1)
        m.sticks((0.0, 0.0), (1.0, 0.0), 0.01, now=1.0)
        self.assertEqual(m.look.slot, None)  # the camera stays where it was
        self.assertEqual(t.log[-1], ("move", 0, round(0.8 + 9 * LOOK_SCALE, 4), 0.8))

    def test_left_stick_drives_the_joystick(self):
        self.m.sticks((0.0, -1.0), (0.0, 0.0), 0.01, now=1.0)
        self.assertEqual(self.t.log[-1], ("move", 0, 0.2, 0.6))


class PadFlagTests(unittest.TestCase):
    def test_gamepad_button_binds_without_mouse_capture(self):
        m = Mapper(FakeTouch(), prof(new_tap(0.5, 0.5, "BTN_SOUTH")))
        self.assertTrue(m.pad)
        self.assertFalse(m.mouse_binds)
        self.assertEqual(m.key(e.BTN_SOUTH, 1), 0)

    def test_mouse_button_still_captures_the_mouse(self):
        m = Mapper(FakeTouch(), prof(new_tap(0.5, 0.5, "BTN_LEFT")))
        self.assertTrue(m.mouse_binds)
        self.assertFalse(m.pad)

    def test_stick_only_joystick_counts_as_pad(self):
        m = Mapper(FakeTouch(), prof(new_joystick(0.2, 0.7, stick="right")))
        self.assertTrue(m.pad)
        self.assertEqual(m.stick_names, {"right"})

    def test_plain_keyboard_profile_has_no_pad(self):
        m = Mapper(FakeTouch(), prof(new_tap(0.5, 0.5, "KEY_J")))
        self.assertFalse(m.pad)
