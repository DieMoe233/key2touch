"""Gamepad routing in the engine: mapped buttons become touches, everything
else is mirrored to a virtual pad (no real devices or uinput here)."""
import time
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from evdev import AbsInfo
from evdev import ecodes as e

from key2touch.engine import Engine
from key2touch.mapper import Mapper
from key2touch.profile import new_joystick, new_look, new_tap
from tests.test_mapper import FakeTouch


def info(lo, hi):
    return AbsInfo(0, lo, hi, 0, 0, 0)


class FakePad:
    fd = 11
    name = "Fake Gamepad"

    def __init__(self):
        self.batch = []
        self.grabbed = False
        self.info = NS(vendor=0x1234, product=0x5678, version=0x110, bustype=3)
        self.abs = {e.ABS_X: info(-1, 1), e.ABS_Y: info(-1, 1),
                    e.ABS_RX: info(-1, 1), e.ABS_RY: info(-1, 1),
                    e.ABS_HAT0X: info(-1, 1), e.ABS_HAT0Y: info(-1, 1),
                    e.ABS_Z: info(0, 255), e.ABS_RZ: info(0, 255)}
        # No BTN_DPAD_* / BTN_TL2: both are translated from axes.
        self.keys = [e.BTN_SOUTH, e.BTN_EAST, e.BTN_TL, e.BTN_TR,
                     e.BTN_SELECT, e.BTN_START]

    def capabilities(self, absinfo=False):
        if absinfo:
            return {e.EV_KEY: list(self.keys), e.EV_ABS: list(self.abs.items())}
        return {e.EV_KEY: list(self.keys), e.EV_ABS: list(self.abs)}

    def read(self):
        batch, self.batch = self.batch, []
        return iter(batch)

    def close(self):
        pass

    def active_keys(self):
        return []

    def grab(self):
        self.grabbed = True

    def ungrab(self):
        self.grabbed = False


class FakeMirror:
    def __init__(self):
        self.log = []

    def write(self, ev_type, code, value):
        self.log.append((ev_type, code, value))

    def syn(self):
        pass

    def close(self):
        pass


def key(code, value):
    return NS(type=e.EV_KEY, code=code, value=value)


def axis(code, value):
    return NS(type=e.EV_ABS, code=code, value=value)


class PadEngine:
    def __init__(self, points, look=False):
        self.touch = FakeTouch()
        eng = Engine.__new__(Engine)  # skip __init__: it opens uinput devices
        eng.touch = self.touch
        eng.mapper = Mapper(self.touch, {"points": points}, aspect=1.0)
        eng.active, eng.enabled = True, True
        eng.pads, eng.pad_caps, eng.pad_raw, eng.pad_down = {}, {}, {}, {}
        eng.pad_mirrors, eng.pad_mirror_at, eng.pads_grabbed = {}, {}, set()
        eng.dev_down, eng.hotkey_down, eng.hotkeys = {}, set(), {}
        eng.hotkeys_on = True
        eng.want_pad_grab = False
        eng.mirror = None
        eng.mouse_mode_now = "look" if look else None
        eng.stick_at = time.monotonic()
        eng.msgs = []
        eng.emit = lambda **kw: eng.msgs.append(kw)
        eng.emit_state = lambda: None
        eng.log = lambda *a: None
        self.eng = eng
        self.dev = FakePad()
        eng.add_pad(self.dev)

    def grab(self):
        mirror = FakeMirror()
        self.eng.pad_mirrors[self.dev.fd] = mirror
        self.eng.pads_grabbed.add(self.dev.fd)
        return mirror

    def feed(self, *events):
        """One read batch, then the per-frame stick tick the main loop does."""
        self.dev.batch = list(events)
        self.eng.on_pad_events(self.dev)
        self.eng.stick_at = time.monotonic() - 0.01
        self.eng.tick_sticks()


class ButtonTests(unittest.TestCase):
    def test_mapped_button_becomes_a_touch(self):
        pad = PadEngine([new_tap(0.5, 0.5, "BTN_SOUTH")])
        mirror = pad.grab()
        pad.feed(key(e.BTN_SOUTH, 1))
        self.assertEqual(pad.touch.live, {0})
        pad.feed(key(e.BTN_SOUTH, 0))
        self.assertEqual(pad.touch.live, set())
        self.assertEqual(mirror.log, [])  # swallowed: the game only sees the touch

    def test_unmapped_button_is_mirrored_while_grabbed(self):
        pad = PadEngine([new_tap(0.5, 0.5, "BTN_SOUTH")])
        mirror = pad.grab()
        pad.feed(key(e.BTN_EAST, 1), key(e.BTN_EAST, 0))
        self.assertEqual(mirror.log, [(e.EV_KEY, e.BTN_EAST, 1),
                                      (e.EV_KEY, e.BTN_EAST, 0)])
        self.assertEqual(pad.touch.log, [])

    def test_nothing_is_mirrored_while_not_grabbed(self):
        pad = PadEngine([new_tap(0.5, 0.5, "BTN_SOUTH")])
        pad.feed(key(e.BTN_EAST, 1))
        self.assertEqual(pad.eng.pad_mirrors, {})
        self.assertEqual(pad.touch.log, [])

    def test_every_button_reaches_the_editor(self):
        pad = PadEngine([], look=True)
        pad.feed(key(e.BTN_START, 1))
        self.assertIn({"ev": "pad", "code": e.BTN_START, "down": True}, pad.eng.msgs)


class AxisTests(unittest.TestCase):
    def test_mapped_stick_becomes_a_touch_and_is_swallowed(self):
        js = new_joystick(0.2, 0.7, r=0.1, stick="left")
        pad = PadEngine([js])
        mirror = pad.grab()
        pad.feed(axis(e.ABS_X, 1))
        self.assertEqual(pad.touch.log[0], ("down", 0, 0.2, 0.7))
        self.assertEqual(pad.touch.log[-1], ("move", 0, 0.3, 0.7))
        self.assertEqual(mirror.log, [])

    def test_unused_axis_is_mirrored(self):
        pad = PadEngine([])
        mirror = pad.grab()
        pad.feed(axis(e.ABS_RX, -1))
        self.assertEqual(mirror.log, [(e.EV_ABS, e.ABS_RX, -1)])

    def test_hat_axis_presses_the_dpad_button(self):
        pad = PadEngine([new_tap(0.9, 0.5, "BTN_DPAD_UP")])
        mirror = pad.grab()
        pad.feed(axis(e.ABS_HAT0Y, -1))
        self.assertEqual(pad.touch.live, {0})
        self.assertFalse(any(t == e.EV_ABS for t, _, _ in mirror.log))
        self.assertIn({"ev": "pad", "code": e.BTN_DPAD_UP, "down": True}, pad.eng.msgs)
        pad.feed(axis(e.ABS_HAT0Y, 0))
        self.assertEqual(pad.touch.live, set())

    def test_trigger_axis_presses_l2(self):
        pad = PadEngine([new_tap(0.9, 0.5, "BTN_TL2")])
        pad.grab()
        pad.feed(axis(e.ABS_Z, 255))
        self.assertEqual(pad.touch.live, {0})
        pad.feed(axis(e.ABS_Z, 0))
        self.assertEqual(pad.touch.live, set())

    def test_right_stick_aims_a_pad_skill_and_is_not_mirrored(self):
        from key2touch.profile import new_skill
        pad = PadEngine([new_skill(0.8, 0.8, "BTN_TL")])
        mirror = pad.grab()
        pad.feed(key(e.BTN_TL, 1))
        pad.feed(axis(e.ABS_RX, 1))
        self.assertEqual(pad.touch.log[0], ("down", 0, 0.8, 0.8))
        self.assertEqual(pad.touch.log[-1][0], "move")
        self.assertFalse(any(t == e.EV_ABS for t, _, _ in mirror.log))

    def test_camera_stick_steers_the_look_area(self):
        from key2touch.profile import new_look
        pad = PadEngine([new_look(0.6, 0.4, r=0.1, stick="right")], look=True)
        pad.eng.mapper.look.r = 0.1
        pad.grab()
        pad.feed(axis(e.ABS_RX, 1))
        self.assertEqual([l[0] for l in pad.touch.log], ["down", "move"])
        self.assertGreater(pad.touch.log[-1][2], 0.6)


class UnplugTests(unittest.TestCase):
    def test_unplugging_lifts_held_touches(self):
        js = new_joystick(0.2, 0.7, r=0.1, stick="left")
        pad = PadEngine([new_tap(0.5, 0.5, "BTN_SOUTH"), js])
        pad.grab()
        pad.feed(key(e.BTN_SOUTH, 1), axis(e.ABS_X, 1))
        self.assertEqual(len(pad.touch.live), 2)
        pad.eng.drop_pad(pad.dev.fd)
        self.assertEqual(pad.touch.live, set())
        self.assertEqual(pad.eng.pads, {})
        self.assertEqual(pad.eng.pad_mirrors, {})


class MirrorTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("key2touch.engine.log")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_mirror_copies_caps_and_adds_synthesised_buttons(self):
        pad = PadEngine([])
        mirror = FakeMirror()
        with mock.patch("key2touch.engine.UInput", return_value=mirror) as ui:
            self.assertIs(pad.eng.ensure_pad_mirror(pad.dev), mirror)
        caps = ui.call_args[0][0]
        for code in (e.BTN_DPAD_UP, e.BTN_DPAD_DOWN, e.BTN_TL2, e.BTN_TR2):
            self.assertIn(code, caps[e.EV_KEY])
        self.assertIn(e.ABS_HAT0Y, dict(caps[e.EV_ABS]))
        self.assertNotIn(e.EV_MSC, caps)
        kwargs = ui.call_args[1]
        self.assertEqual((kwargs["vendor"], kwargs["product"]), (0x1234, 0x5678))

    def test_grab_waits_for_the_mirror_to_be_seen(self):
        pad = PadEngine([])
        pad.eng.want_pad_grab = True
        pad.eng.devices_ready_at = 0
        mirror = FakeMirror()
        with mock.patch("key2touch.engine.UInput", return_value=mirror):
            pad.eng.try_grab_pad(pad.dev)
            self.assertFalse(pad.dev.grabbed)  # the mirror needs a moment first
            pad.eng.pad_mirror_at[pad.dev.fd] -= 1.0
            pad.eng.try_grab_pad(pad.dev)
            self.assertTrue(pad.dev.grabbed)
            pad.eng.pad_down[pad.dev.fd].add(e.BTN_START)
            pad.eng.release_pads()
        self.assertFalse(pad.dev.grabbed)
        self.assertEqual(pad.eng.pad_down[pad.dev.fd], set())
        self.assertEqual(mirror.log[-1], (e.EV_KEY, e.BTN_START, 0))

    def test_padless_profile_leaves_the_pad_alone(self):
        pad = PadEngine([new_tap(0.5, 0.5, "KEY_J")])
        pad.eng.want_grab = True
        pad.eng.update_pad_grab()
        self.assertFalse(pad.eng.want_pad_grab)

    def test_profile_with_a_pad_button_wants_the_pad(self):
        pad = PadEngine([new_tap(0.5, 0.5, "BTN_SOUTH")])
        pad.eng.want_grab = True
        pad.eng.devices_ready_at = 0
        with mock.patch("key2touch.engine.UInput", return_value=FakeMirror()):
            pad.eng.update_pad_grab()
        self.assertTrue(pad.eng.want_pad_grab)
        self.assertIn(pad.dev.fd, pad.eng.pad_mirrors)


if __name__ == "__main__":
    unittest.main()
