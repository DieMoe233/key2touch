import unittest

from evdev import AbsInfo
from evdev import ecodes as e

from key2touch import gamepad


def info(lo, hi):
    return AbsInfo(0, lo, hi, 0, 0, 0)


class NameTests(unittest.TestCase):
    def test_short_names_round_trip(self):
        for short in ("south", "east", "north", "west", "l1", "r1", "l2", "r2",
                      "l3", "r3", "select", "start", "guide",
                      "dpad-up", "dpad-down", "dpad-left", "dpad-right"):
            name = gamepad.name(short)
            self.assertEqual(gamepad.short(name), short)
            self.assertTrue(gamepad.label(short))

    def test_raw_names_and_codes(self):
        self.assertEqual(gamepad.code("BTN_SOUTH"), e.BTN_SOUTH)
        self.assertEqual(gamepad.code(e.BTN_SOUTH), e.BTN_SOUTH)
        self.assertEqual(gamepad.name(e.BTN_EAST), "BTN_EAST")
        self.assertEqual(gamepad.label("BTN_TL"), "LB")
        self.assertEqual(gamepad.code("lb"), e.BTN_TL)
        self.assertIsNone(gamepad.code("KEY_A"))
        self.assertIsNone(gamepad.code(""))
        self.assertIsNone(gamepad.name(e.BTN_LEFT))  # mouse buttons are not pad buttons

    def test_extra_buttons(self):
        self.assertEqual(gamepad.code("BTN_TRIGGER_HAPPY1"), e.BTN_TRIGGER_HAPPY1)
        self.assertEqual(gamepad.name(e.BTN_TRIGGER_HAPPY1), "BTN_TRIGGER_HAPPY1")
        self.assertIsNone(gamepad.short(e.BTN_TRIGGER_HAPPY1))
        self.assertTrue(gamepad.is_button(e.BTN_TRIGGER_HAPPY1))

    def test_is_button(self):
        self.assertTrue(gamepad.is_button(e.BTN_SOUTH))
        self.assertTrue(gamepad.is_button(e.BTN_DPAD_UP))
        self.assertFalse(gamepad.is_button(e.BTN_LEFT))
        self.assertFalse(gamepad.is_button(e.KEY_A))


class StickAxisTests(unittest.TestCase):
    def test_modern_pad(self):
        absinfo = {e.ABS_X: info(-32768, 32767), e.ABS_Y: info(-32768, 32767),
                   e.ABS_RX: info(-32768, 32767), e.ABS_RY: info(-32768, 32767),
                   e.ABS_Z: info(0, 255), e.ABS_RZ: info(0, 255)}
        sticks = gamepad.stick_axes(absinfo)
        self.assertEqual(sticks["left"], (e.ABS_X, e.ABS_Y))
        self.assertEqual(sticks["right"], (e.ABS_RX, e.ABS_RY))

    def test_old_pad_right_stick_on_z(self):
        absinfo = {e.ABS_X: info(-1, 1), e.ABS_Y: info(-1, 1),
                   e.ABS_Z: info(-32768, 32767), e.ABS_RZ: info(-32768, 32767)}
        self.assertEqual(gamepad.stick_axes(absinfo)["right"], (e.ABS_Z, e.ABS_RZ))

    def test_xbox_bluetooth_pad(self):
        # Real caps of an Xbox Wireless Controller over Bluetooth: unsigned
        # sticks, right stick on ABS_Z/ABS_RZ, triggers on ABS_GAS/BRAKE.
        absinfo = {e.ABS_X: info(0, 65535), e.ABS_Y: info(0, 65535),
                   e.ABS_Z: info(0, 65535), e.ABS_RZ: info(0, 65535),
                   e.ABS_GAS: info(0, 1023), e.ABS_BRAKE: info(0, 1023),
                   e.ABS_HAT0X: info(-1, 1), e.ABS_HAT0Y: info(-1, 1)}
        sticks = gamepad.stick_axes(absinfo)
        self.assertEqual(sticks["left"], (e.ABS_X, e.ABS_Y))
        self.assertEqual(sticks["right"], (e.ABS_Z, e.ABS_RZ))
        keys = {e.BTN_SOUTH, e.BTN_TL2, e.BTN_TR2}
        ab = gamepad.AxisButtons(absinfo, keys, sticks)
        self.assertNotIn(e.ABS_Z, ab.triggers)   # the right stick, not a trigger
        self.assertNotIn(e.ABS_RZ, ab.triggers)
        self.assertEqual(ab.feed(e.ABS_HAT0Y, 1), [(e.BTN_DPAD_DOWN, True)])

    def test_generic_pad_triggers_from_gas_brake(self):
        absinfo = {e.ABS_X: info(0, 65535), e.ABS_Y: info(0, 65535),
                   e.ABS_Z: info(0, 65535), e.ABS_RZ: info(0, 65535),
                   e.ABS_GAS: info(0, 1023), e.ABS_BRAKE: info(0, 1023)}
        sticks = gamepad.stick_axes(absinfo)
        ab = gamepad.AxisButtons(absinfo, {e.BTN_SOUTH}, sticks)
        self.assertIn(e.ABS_GAS, ab.triggers)
        self.assertIn(e.ABS_BRAKE, ab.triggers)
        self.assertEqual(ab.feed(e.ABS_GAS, 900), [(e.BTN_TL2, True)])

    def test_unsigned_trigger_axes_are_not_a_stick(self):
        absinfo = {e.ABS_X: info(-1, 1), e.ABS_Y: info(-1, 1),
                   e.ABS_Z: info(0, 255), e.ABS_RZ: info(0, 255)}
        self.assertNotIn("right", gamepad.stick_axes(absinfo))

    def test_no_axes(self):
        self.assertEqual(gamepad.stick_axes({}), {})

    def test_normalize(self):
        self.assertEqual(gamepad.normalize(info(-1, 1), -1), -1)
        self.assertEqual(gamepad.normalize(info(-1, 1), 1), 1)
        self.assertEqual(gamepad.normalize(info(-1, 1), 0), 0)
        self.assertEqual(gamepad.normalize(info(5, 5), 5), 0.0)
        self.assertGreater(gamepad.normalize(info(0, 255), 255), 0.99)


class AxisButtonTests(unittest.TestCase):
    def test_hat_becomes_dpad(self):
        absinfo = {e.ABS_HAT0X: info(-1, 1), e.ABS_HAT0Y: info(-1, 1)}
        ab = gamepad.AxisButtons(absinfo, set())
        self.assertEqual(ab.feed(e.ABS_HAT0X, 1), [(e.BTN_DPAD_RIGHT, True)])
        self.assertEqual(ab.feed(e.ABS_HAT0X, 0), [(e.BTN_DPAD_RIGHT, False)])
        self.assertEqual(ab.feed(e.ABS_HAT0Y, -1), [(e.BTN_DPAD_UP, True)])
        self.assertEqual(ab.feed(e.ABS_HAT0Y, -1), [])  # unchanged: no repeat
        self.assertIn(e.ABS_HAT0X, ab.axes)

    def test_pads_with_dpad_buttons_keep_the_hat(self):
        absinfo = {e.ABS_HAT0X: info(-1, 1), e.ABS_HAT0Y: info(-1, 1)}
        ab = gamepad.AxisButtons(absinfo, {e.BTN_DPAD_UP, e.BTN_DPAD_DOWN,
                                           e.BTN_DPAD_LEFT, e.BTN_DPAD_RIGHT})
        self.assertEqual(ab.feed(e.ABS_HAT0X, 1), [])
        self.assertFalse(ab.axes)

    def test_trigger_hysteresis(self):
        absinfo = {e.ABS_Z: info(0, 255)}
        ab = gamepad.AxisButtons(absinfo, set())
        self.assertEqual(ab.feed(e.ABS_Z, 100), [])  # below 55 %
        self.assertEqual(ab.feed(e.ABS_Z, 200), [(e.BTN_TL2, True)])
        self.assertEqual(ab.feed(e.ABS_Z, 120), [])  # above the release point
        self.assertEqual(ab.feed(e.ABS_Z, 50), [(e.BTN_TL2, False)])

    def test_digital_trigger_buttons_win(self):
        absinfo = {e.ABS_Z: info(0, 255), e.ABS_RZ: info(0, 255)}
        ab = gamepad.AxisButtons(absinfo, {e.BTN_TL2, e.BTN_TR2})
        self.assertEqual(ab.feed(e.ABS_Z, 255), [])
        self.assertFalse(ab.axes)
        self.assertEqual(ab.codes(), set())


if __name__ == "__main__":
    unittest.main()
