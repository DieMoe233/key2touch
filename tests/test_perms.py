import grp
import os
import unittest
from unittest import mock

from key2touch import perms


def group(members, gid=4242):
    return grp.struct_group(("input", "x", gid, members))


class StatusTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("KEY2TOUCH_FAKE_PERMS", None)
        self.me = perms.user()

    def status(self, access, members, session, gid=4242):
        return perms.status(can_access=lambda: access, getgrnam=lambda _: group(members, gid), session_gids=lambda: session)

    def test_access_works(self):
        self.assertEqual(self.status(True, [], []), perms.OK)

    def test_not_a_member(self):
        self.assertEqual(self.status(False, ["someone"], []), perms.NEED_GROUP)

    def test_added_but_session_is_older(self):
        self.assertEqual(self.status(False, [self.me], [1000]), perms.RELOGIN)

    def test_member_in_session_but_still_denied(self):
        self.assertEqual(self.status(False, [self.me], [4242]), perms.BROKEN)

    def test_primary_group_counts_as_member(self):
        self.assertEqual(self.status(False, [], [], gid=os.getgid()), perms.RELOGIN)

    def test_no_input_group(self):
        def missing(_):
            raise KeyError("input")
        self.assertEqual(perms.status(can_access=lambda: False, getgrnam=missing), perms.NEED_GROUP)

    def test_preview_override(self):
        with mock.patch.dict(os.environ, {"KEY2TOUCH_FAKE_PERMS": "relogin"}):
            self.assertEqual(self.status(True, [], []), perms.RELOGIN)

    def test_grant_command_targets_this_user(self):
        self.assertEqual(perms.grant_cmd(), ["pkexec", "usermod", "-aG", "input", self.me])


if __name__ == "__main__":
    unittest.main()
