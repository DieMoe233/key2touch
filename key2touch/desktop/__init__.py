"""Desktop backends: everything compositor-specific lives here.

The engine and touch device are compositor-agnostic (evdev / uinput /
libinput).  A backend only supplies the extras:

* focus tracking (engine side): which app_id has keyboard focus, so mapping
  can follow the game window;
* window list (GUI side): app_ids to pick from;
* touch → output mapping: which monitor the virtual touchscreen covers.

Outputs themselves come from GDK in the GUI, which works everywhere.
"""

import glob
import os

# Every IPC socket gets a timeout: the engine may hold the keyboard grab while
# it talks to the compositor, and a blocked engine would mean a dead keyboard.
IPC_TIMEOUT = 1.0


class Backend:
    name = "generic"
    label = "通用"
    supports_focus = False

    # ---- engine side ----
    def focus_source(self):
        """An object with fileno() and read() -> [app_id | None, ...], or None."""
        return None

    # ---- GUI side ----
    def windows(self):
        """[{'app_id': str, 'title': str, 'output': str | None}]"""
        return []

    def outputs(self):
        """name -> {'width', 'height', 'scale'} in logical pixels (empty dict if unknown).

        Generic fallback: connected DRM connectors from sysfs, which carry the
        same names Wayland compositors use (DP-2, eDP-1, HDMI-A-1 ...)."""
        out = {}
        for status in glob.glob("/sys/class/drm/card*-*/status"):
            try:
                if open(status).read().strip() == "connected":
                    out[os.path.basename(os.path.dirname(status)).split("-", 1)[1]] = {}
            except OSError:
                pass
        return out

    def focused_output(self):
        return None

    def notice(self, output, n_outputs):
        """Something the user must act on: (text, button label or None), or None."""
        return None

    def setup_text(self, output):
        return ""

    def setup(self, output):
        pass

    def touch_sync(self, output):
        """Called whenever the profile is applied; make the mapping current."""


def detect():
    forced = os.environ.get("KEY2TOUCH_DESKTOP", "").lower()
    from . import hyprland, niri, sway
    candidates = {"niri": niri.NiriBackend, "hyprland": hyprland.HyprlandBackend,
                  "sway": sway.SwayBackend}
    if forced in candidates:
        return candidates[forced]()
    if forced == "generic":
        return Backend()
    for cls in (niri.NiriBackend, hyprland.HyprlandBackend, sway.SwayBackend):
        if cls.available():
            return cls()
    return Backend()
