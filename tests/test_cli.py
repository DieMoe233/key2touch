import unittest

from key2touch.cli import CliError, fmt_key, parse_coord, parse_key


class KeySpecTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_key("j"), ("KEY_J", []))
        self.assertEqual(parse_key("Ctrl+Shift+Q"), ("KEY_Q", ["CTRL", "SHIFT"]))
        self.assertEqual(parse_key("super+lshift"), ("KEY_LEFTSHIFT", ["META"]))
        self.assertEqual(parse_key("KEY_SPACE"), ("KEY_SPACE", []))
        self.assertEqual(parse_key("esc"), ("KEY_ESC", []))

    def test_round_trip(self):
        for spec in ("j", "ctrl+q", "alt+shift+1", "lshift", "up", "f12", "space", "kp5"):
            self.assertEqual(fmt_key(*parse_key(spec)), spec)

    def test_bad(self):
        for bad in ("", "nosuchkey", "hyper+j", "↑"):
            with self.assertRaises(CliError):
                parse_key(bad)

    def test_coords(self):
        self.assertEqual(parse_coord("0.25"), 0.25)
        self.assertEqual(parse_coord("80%"), 0.8)
        for bad in ("1.5", "-1", "abc", "120%"):
            with self.assertRaises(CliError):
                parse_coord(bad)


if __name__ == "__main__":
    unittest.main()


class CheckTests(unittest.TestCase):
    def test_check(self):
        from key2touch.cli import check_profile
        from key2touch.desktop import Backend
        from key2touch.profile import new_joystick, new_tap, normalize

        class Fake(Backend):
            supports_focus = True

            def outputs(self):
                return {"DP-2": {"width": 2560, "height": 1440, "scale": 1}, "eDP-1": {}}

            def windows(self):
                return [{"app_id": "Waydroid", "title": "", "output": "DP-2"}]

        p = normalize({"output": "eDP-1", "points": [
            new_joystick(0.15, 0.7), new_tap(0.16, 0.7, "KEY_J"), new_tap(0.5, 0.5), new_tap(1.2, 0.5, "KEY_K")]})
        codes = {(x["level"], x["code"]) for x in check_profile("t", p, Fake())}
        self.assertIn(("error", "window_on_other_output"), codes)
        self.assertIn(("error", "out_of_range"), codes)
        self.assertIn(("warning", "unbound"), codes)
        self.assertIn(("warning", "points_inside"), codes)

        p["output"] = "DP-9"
        self.assertIn(("error", "output_missing"), {(x["level"], x["code"]) for x in check_profile("t", p, Fake())})
