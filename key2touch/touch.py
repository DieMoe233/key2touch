"""Virtual multitouch screen on /dev/uinput (MT protocol B)."""

from evdev import UInput, AbsInfo, ecodes as e

ABS_MAX = 65535
SLOTS = 10
DEVICE_NAME = "key2touch virtual touchscreen"
# pid.codes test IDs; fixed so compositors can match the device by identifier.
VENDOR, PRODUCT = 0x1209, 0x0001


class TouchScreen:
    def __init__(self):
        absi = AbsInfo(value=0, min=0, max=ABS_MAX, fuzz=0, flat=0, resolution=0)
        caps = {
            e.EV_KEY: [e.BTN_TOUCH],
            e.EV_ABS: [
                (e.ABS_X, absi),
                (e.ABS_Y, absi),
                (e.ABS_MT_SLOT, AbsInfo(0, 0, SLOTS - 1, 0, 0, 0)),
                (e.ABS_MT_TRACKING_ID, AbsInfo(0, 0, 65535, 0, 0, 0)),
                (e.ABS_MT_POSITION_X, absi),
                (e.ABS_MT_POSITION_Y, absi),
            ],
        }
        # INPUT_PROP_DIRECT makes libinput treat it as a touchscreen (wl_touch)
        # instead of an absolute pointer.
        self.ui = UInput(caps, name=DEVICE_NAME, vendor=VENDOR, product=PRODUCT,
                         input_props=[e.INPUT_PROP_DIRECT])
        self.free = list(range(SLOTS))
        self.active = {}  # slot -> True
        self.next_tid = 1
        self.cur_slot = -1

    @staticmethod
    def _abs(v):
        v = min(max(v, 0.0), 1.0)
        return int(round(v * ABS_MAX))

    def _slot(self, s):
        if s != self.cur_slot:
            self.ui.write(e.EV_ABS, e.ABS_MT_SLOT, s)
            self.cur_slot = s

    def down(self, x, y):
        """Start a contact; returns slot or None if all slots busy."""
        if not self.free:
            return None
        s = self.free.pop(0)
        self._slot(s)
        self.ui.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, self.next_tid)
        self.next_tid = self.next_tid % 65535 + 1
        self.ui.write(e.EV_ABS, e.ABS_MT_POSITION_X, self._abs(x))
        self.ui.write(e.EV_ABS, e.ABS_MT_POSITION_Y, self._abs(y))
        if not self.active:
            self.ui.write(e.EV_KEY, e.BTN_TOUCH, 1)
            self.ui.write(e.EV_ABS, e.ABS_X, self._abs(x))
            self.ui.write(e.EV_ABS, e.ABS_Y, self._abs(y))
        self.active[s] = True
        self.ui.syn()
        return s

    def move(self, s, x, y):
        if s not in self.active:
            return
        self._slot(s)
        self.ui.write(e.EV_ABS, e.ABS_MT_POSITION_X, self._abs(x))
        self.ui.write(e.EV_ABS, e.ABS_MT_POSITION_Y, self._abs(y))
        self.ui.syn()

    def up(self, s):
        if s not in self.active:
            return
        self._slot(s)
        self.ui.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, -1)
        del self.active[s]
        self.free.append(s)
        if not self.active:
            self.ui.write(e.EV_KEY, e.BTN_TOUCH, 0)
        self.ui.syn()

    def release_all(self):
        for s in list(self.active):
            self.up(s)

    def close(self):
        self.release_all()
        self.ui.close()
