"""Desktop backends: everything compositor-specific lives here.

The engine and touch device are compositor-agnostic (evdev / uinput /
libinput).  A backend only supplies the extras:

* focus tracking (engine side): which app_id has keyboard focus, so mapping
  can follow the game window;
* window list (GUI side): app_ids to pick from;
* touch → output mapping: which monitor the virtual touchscreen covers.

Outputs themselves come from GDK in the GUI, which works everywhere.
"""

import os


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
        """[{'app_id': str, 'title': str}]"""
        return []

    def focused_output(self):
        return None

    def touch_status(self, output, n_outputs):
        """-> (ok, subtitle, setup button label or None)"""
        if n_outputs <= 1:
            return True, "只有一个输出，无需映射", None
        return None, (f"请在桌面的输入设备设置里把「key2touch virtual touchscreen」映射到 {output}，"
                      "否则触摸会落在默认输出上"), None

    def touch_setup_text(self, output):
        return ""

    def touch_setup(self, output):
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
