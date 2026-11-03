"""Protocol tests for the Hyprland / Sway backends against fake IPC servers."""

import json
import os
import select
import socket
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from key2touch.desktop import hyprland, sway


def serve(path, handler):
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen()

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=handler, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return srv


def read_ready(src, timeout=2):
    r, _, _ = select.select([src], [], [], timeout)
    assert r, "no event"
    return src.read()


class HyprlandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name) / "hypr" / "SIG"
        d.mkdir(parents=True)
        self.commands = []
        self.events = threading.Event()
        replies = {
            "j/clients": [{"class": "Waydroid", "title": "Waydroid"}, {"class": "firefox", "title": "x"}],
            "j/monitors": [{"name": "eDP-1", "focused": False}, {"name": "DP-2", "focused": True}],
            "j/activewindow": {"class": "firefox"},
        }

        def req(conn):
            cmd = conn.recv(4096).decode()
            self.commands.append(cmd)
            conn.sendall(b"ok" if cmd.startswith("keyword") else json.dumps(replies[cmd]).encode())
            conn.close()

        def events(conn):
            self.events.wait(2)
            conn.sendall(b"workspace>>2\nactivewindow>>Waydroid,Waydroid\nactivewindow>>,\n")

        self.srvs = [serve(d / ".socket.sock", req), serve(d / ".socket2.sock", events)]
        self.env = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name,
                                                "HYPRLAND_INSTANCE_SIGNATURE": "SIG"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        for s in self.srvs:
            s.close()
        self.tmp.cleanup()

    def test_requests(self):
        b = hyprland.HyprlandBackend()
        self.assertTrue(b.available())
        self.assertEqual([w["app_id"] for w in b.windows()], ["Waydroid", "firefox"])
        self.assertEqual(b.focused_output(), "DP-2")
        b.touch_sync("DP-2")
        self.assertIn("keyword device[key2touch-virtual-touchscreen]:output DP-2", self.commands)
        self.assertTrue(b.touch_status("DP-2", 2)[0])

    def test_focus_events(self):
        f = hyprland.HyprlandBackend().focus_source()
        self.assertEqual(f.initial_app, "firefox")
        self.events.set()
        got = []
        while len(got) < 2:
            got += read_ready(f)
        self.assertEqual(got, ["Waydroid", None])


class SwayTests(unittest.TestCase):
    TREE = {"nodes": [{"nodes": [
        {"pid": 1, "app_id": "Waydroid", "name": "Waydroid", "focused": True},
        {"pid": 2, "app_id": None, "window_properties": {"class": "Steam"}, "name": "Steam"},
    ]}]}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "sway.sock"
        self.commands = []
        hdr = sway.HEADER

        def send(conn, t, obj):
            body = json.dumps(obj).encode()
            conn.sendall(hdr.pack(sway.MAGIC, len(body), t) + body)

        def handle(conn):
            while True:
                head = conn.recv(hdr.size)
                if len(head) < hdr.size:
                    return
                _, n, t = hdr.unpack(head)
                payload = conn.recv(n).decode() if n else ""
                if t == sway.GET_TREE:
                    send(conn, t, self.TREE)
                elif t == sway.GET_OUTPUTS:
                    send(conn, t, [{"name": "DP-2", "focused": True}])
                elif t == sway.RUN_COMMAND:
                    self.commands.append(payload)
                    send(conn, t, [{"success": True}])
                elif t == sway.SUBSCRIBE:
                    send(conn, t, {"success": True})
                    send(conn, sway.EVENT_WINDOW, {"change": "focus",
                                                   "container": {"app_id": None, "window_properties": {"class": "Steam"}}})
                    send(conn, sway.EVENT_WORKSPACE, {"change": "focus"})

        self.srv = serve(path, handle)
        self.env = mock.patch.dict(os.environ, {"SWAYSOCK": str(path)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.srv.close()
        self.tmp.cleanup()

    def test_requests(self):
        b = sway.SwayBackend()
        self.assertTrue(b.available())
        self.assertEqual([w["app_id"] for w in b.windows()], ["Waydroid", "Steam"])
        self.assertEqual(b.focused_output(), "DP-2")
        b.touch_sync("DP-2")
        self.assertEqual(self.commands, ['input "4617:1:key2touch_virtual_touchscreen" map_to_output DP-2'])
        self.assertTrue(b.touch_status("DP-2", 2)[0])

    def test_focus_events(self):
        f = sway.SwayBackend().focus_source()
        self.assertEqual(f.initial_app, "Waydroid")
        self.assertEqual(read_ready(f), ["Steam"])  # Xwayland class fallback
        self.assertEqual(read_ready(f), ["Waydroid"])  # workspace focus re-queries the tree


if __name__ == "__main__":
    unittest.main()
