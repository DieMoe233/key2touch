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
