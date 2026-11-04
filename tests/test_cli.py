import unittest
from pathlib import Path

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


class SkillCliTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from unittest import mock
        from key2touch import cli, profile
        d = Path(tempfile.mkdtemp())
        for name, val in (("CONFIG_DIR", d), ("PROFILE_DIR", d / "profiles"), ("SETTINGS_FILE", d / "settings.json")):
            p = mock.patch.object(profile, name, val)
            p.start()
            self.addCleanup(p.stop)
        r = mock.patch.object(cli, "running", lambda: False)  # never talk to a real running app
        r.start()
        self.addCleanup(r.stop)
        self.cli, self.profile = cli, profile
        self.run_cli("profile", "new", "moba")

    def run_cli(self, *argv):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = self.cli.main([*argv])
        return code, buf.getvalue()

    def test_add_list_set(self):
        code, _ = self.run_cli("point", "add", "skill", "q", "70%", "80%", "--cancel", "85%,20%", "-p", "moba")
        self.assertEqual(code, 0)
        pt = self.profile.load("moba")["points"][0]
        self.assertEqual((pt["type"], pt["key"], pt["cancel"]), ("skill", "KEY_Q", [0.85, 0.2]))
        code, out = self.run_cli("point", "list", "-p", "moba", "--json")
        import json
        d = json.loads(out)[0]
        self.assertEqual((d["type"], d["key"], d["cancel"], d["size"]), ("skill", "q", [0.85, 0.2], 0.1))
        self.assertEqual(self.run_cli("point", "set", "0", "--cancel", "none", "--sens", "2", "-p", "moba")[0], 0)
        pt = self.profile.load("moba")["points"][0]
        self.assertEqual((pt["cancel"], pt["sens"]), (None, 2.0))

    def test_key_moves_between_tap_and_skill(self):
        self.run_cli("point", "add", "tap", "q", "10%", "10%", "-p", "moba")
        self.assertEqual(self.run_cli("point", "add", "skill", "q", "70%", "80%", "-p", "moba")[0], 1)  # clash
        self.assertEqual(self.run_cli("point", "add", "skill", "q", "70%", "80%", "-p", "moba", "--replace")[0], 0)
        pts = self.profile.load("moba")["points"]
        self.assertEqual([q["type"] for q in pts], ["skill"])  # the keyless tap is gone

    def test_import_accepts_every_point_type(self):
        import io
        import json
        from unittest import mock
        data = {"points": [self.profile.new_skill(0.7, 0.8, "KEY_Q"), self.profile.new_look(0.6, 0.4),
                           self.profile.new_joystick(0.2, 0.7), self.profile.new_tap(0.9, 0.9, "KEY_J")]}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(data))):
            self.assertEqual(self.run_cli("profile", "import", "copy", "-")[0], 0)
        self.assertEqual(len(self.profile.load("copy")["points"]), 4)

    def test_check_skill(self):
        from key2touch.cli import check_profile
        from key2touch.desktop import Backend
        from key2touch.profile import new_skill, normalize

        class Fake(Backend):
            def outputs(self):
                return {"DP-2": {"width": 1920, "height": 1080, "scale": 1}}

            def windows(self):
                return []

        # Two skills side by side: their drag ranges overlap, which is normal.
        p = normalize({"output": "DP-2", "points": [new_skill(0.70, 0.8, "KEY_Q"), new_skill(0.78, 0.8, "KEY_E")]})
        codes = {x["code"] for x in check_profile("t", p, Fake())}
        self.assertIn("skill_no_cancel", codes)
        self.assertNotIn("points_overlap", codes)
        p["points"][0]["cancel"] = [0.8, 0.2]
        self.assertNotIn("skill_no_cancel", {x["code"] for x in check_profile("t", p, Fake())})
