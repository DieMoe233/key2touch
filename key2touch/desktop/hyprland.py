"""Hyprland backend: hyprctl socket for requests, socket2 for focus events."""

import json
import os
import socket
from pathlib import Path

from ..touch import DEVICE_NAME
from . import IPC_TIMEOUT, Backend

# Hyprland names devices lowercased with spaces replaced by dashes.
HYPR_DEVICE = DEVICE_NAME.lower().replace(" ", "-")


def _dir():
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not sig:
        return None
    run = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "hypr" / sig
    return run if run.exists() else Path("/tmp/hypr") / sig  # pre-0.40 location


def _sock_path(name):
    d = _dir()
    if d is None:
        raise FileNotFoundError("HYPRLAND_INSTANCE_SIGNATURE 未设置")
    return str(d / name)


def request(cmd):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(IPC_TIMEOUT)
        s.connect(_sock_path(".socket.sock"))
        s.sendall(cmd.encode())
        chunks = []
        while data := s.recv(65536):
            chunks.append(data)
    return b"".join(chunks).decode()


def request_json(cmd):
    return json.loads(request(f"j/{cmd}") or "null")


class HyprlandFocus:
    def __init__(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(IPC_TIMEOUT)
        self.sock.connect(_sock_path(".socket2.sock"))
        self.buf = b""
        aw = request_json("activewindow") or {}
        self.initial_app = aw.get("class") or None

    def fileno(self):
        return self.sock.fileno()

    def read(self):
        out = []
        data = self.sock.recv(65536)
        if not data:
            raise EOFError("Hyprland event socket closed")
        self.buf += data
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            ev, _, arg = line.decode(errors="replace").partition(">>")
            if ev == "activewindow":
                out.append(arg.split(",", 1)[0] or None)
        return out


class HyprlandBackend(Backend):
    name = "hyprland"
    label = "Hyprland"
    supports_focus = True

    def __init__(self):
        self.sync_error = None

    @staticmethod
    def available():
        d = _dir()
        return bool(d and (d / ".socket.sock").exists())

    def focus_source(self):
        return HyprlandFocus()

    def windows(self):
        try:
            mons = {m["id"]: m["name"] for m in request_json("monitors") or []}
            focused = (request_json("activewindow") or {}).get("address")
            return [{"app_id": c.get("class"), "title": c.get("title") or "",
                     "output": mons.get(c.get("monitor")), "focused": c.get("address") == focused}
                    for c in request_json("clients") or []]
        except (OSError, ValueError, KeyError, TypeError):
            return []

    def outputs(self):
        try:
            # hyprctl reports pixels; logical size is pixels / scale.
            return {m["name"]: {"width": round(m["width"] / m["scale"]), "height": round(m["height"] / m["scale"]),
                                "scale": m["scale"]} for m in request_json("monitors") or []}
        except (OSError, ValueError, KeyError, TypeError):
            return super().outputs()

    def focused_output(self):
        try:
            return next((m["name"] for m in request_json("monitors") if m.get("focused")), None)
        except (OSError, ValueError, TypeError):
            return None

    def touch_sync(self, output):
        # Per-device runtime setting: no config file is touched, and it only
        # affects our virtual device, not real touchscreens.
        try:
            reply = request(f"keyword device[{HYPR_DEVICE}]:output {output}").strip()
            self.sync_error = None if reply == "ok" else reply
        except OSError as ex:
            self.sync_error = str(ex)

    def notice(self, output, n_outputs):
        if self.sync_error and n_outputs > 1:
            return f"无法通过 hyprctl 把触摸映射到 {output}：{self.sync_error}", None
        return None
