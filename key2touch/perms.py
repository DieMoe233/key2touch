"""Can this session read keyboards and create the virtual touchscreen?

Both need the ``input`` group: /dev/input/event* belong to it, and our udev
rule gives it /dev/uinput.  Joining the group only takes effect in a new
login session, so "in /etc/group" and "in this session" are told apart.
"""

import glob
import grp
import os
import pwd

GROUP = "input"

OK = "ok"
NEED_GROUP = "need_group"  # not a member yet
RELOGIN = "relogin"  # member in /etc/group, but this session started before that
BROKEN = "broken"  # member in this session and still denied: uinput module / udev rule


def user():
    return pwd.getpwuid(os.getuid()).pw_name


def _can_access():
    events = glob.glob("/dev/input/event*")
    return os.access("/dev/uinput", os.W_OK) and (not events or any(os.access(p, os.R_OK) for p in events))


def status(can_access=_can_access, getgrnam=grp.getgrnam, session_gids=os.getgroups):
    # Lets the setup page be previewed without touching the real group.
    fake = os.environ.get("KEY2TOUCH_FAKE_PERMS")
    if fake in (NEED_GROUP, RELOGIN, BROKEN):
        return fake
    if can_access():
        return OK
    try:
        g = getgrnam(GROUP)
    except KeyError:
        return NEED_GROUP  # usermod -aG fails without the group; the error is shown then
    member = user() in g.gr_mem or pwd.getpwuid(os.getuid()).pw_gid == g.gr_gid
    if not member:
        return NEED_GROUP
    if g.gr_gid not in session_gids():
        return RELOGIN
    return BROKEN


def grant_cmd():
    return ["pkexec", "usermod", "-aG", GROUP, user()]


def repair_cmd():
    """Load uinput and re-apply our udev rule (what the package's post_install does)."""
    return ["pkexec", "sh", "-c", "modprobe uinput; udevadm trigger --name-match=uinput"]


def manual_cmd():
    return f"sudo usermod -aG {GROUP} $USER"
