"""Sway (and other i3-ipc compositors) backend over $SWAYSOCK."""

import json
import os
import socket
import struct

from ..touch import DEVICE_NAME, PRODUCT, VENDOR
from . import IPC_TIMEOUT, Backend

MAGIC = b"i3-ipc"
HEADER = struct.Struct("<6sII")
RUN_COMMAND, SUBSCRIBE, GET_OUTPUTS, GET_TREE = 0, 2, 3, 4
EVENT_WORKSPACE, EVENT_WINDOW = 0x80000000, 0x80000003
# sway input identifier: "<vendor>:<product>:<name with spaces as _>", decimal.
IDENTIFIER = f"{VENDOR}:{PRODUCT}:{DEVICE_NAME.replace(' ', '_')}"


def _connect():
    path = os.environ.get("SWAYSOCK")
    if not path:
        raise FileNotFoundError("SWAYSOCK 未设置")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(IPC_TIMEOUT)  # also bounds a partial event read on the subscription
    s.connect(path)
    return s


def _send(s, t, payload=""):
    body = payload.encode()
    s.sendall(HEADER.pack(MAGIC, len(body), t) + body)


def _recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise EOFError("sway IPC closed")
        buf += chunk
    return buf


def _recv(s):
    magic, n, t = HEADER.unpack(_recv_exact(s, HEADER.size))
    return t, json.loads(_recv_exact(s, n))


def request(t, payload=""):
    with _connect() as s:
        _send(s, t, payload)
        return _recv(s)[1]


def _leaves(node):
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        if child.get("pid"):
            yield child
        yield from _leaves(child)


def _app_id(con):
    return con.get("app_id") or (con.get("window_properties") or {}).get("class")


def focused_app():
    return next((_app_id(c) for c in _leaves(request(GET_TREE)) if c.get("focused")), None)


class SwayFocus:
    def __init__(self):
        self.sock = _connect()
        _send(self.sock, SUBSCRIBE, json.dumps(["window", "workspace"]))
        _recv(self.sock)  # subscribe reply
        self.initial_app = focused_app()

    def fileno(self):
        return self.sock.fileno()

    def read(self):
        out = []
        t, ev = _recv(self.sock)
        if t == EVENT_WINDOW and ev.get("change") in ("focus", "close", "title"):
            out.append(focused_app() if ev["change"] == "close" else _app_id(ev.get("container") or {}))
        elif t == EVENT_WORKSPACE and ev.get("change") == "focus":
            out.append(focused_app())  # may be an empty workspace
        return out


class SwayBackend(Backend):
    name = "sway"
    label = "Sway"
    supports_focus = True

    def __init__(self):
        self.sync_error = None

    @staticmethod
    def available():
        return bool(os.environ.get("SWAYSOCK")) and os.path.exists(os.environ["SWAYSOCK"])

    def focus_source(self):
        return SwayFocus()

    def windows(self):
        try:
            out = []
            for o in request(GET_TREE).get("nodes", []):
                if o.get("type") != "output" or o.get("name", "").startswith("__"):
                    continue
                out += [{"app_id": _app_id(c), "title": c.get("name") or "", "output": o["name"],
                         "focused": c.get("focused", False)} for c in _leaves(o)]
            return out
        except (OSError, EOFError, ValueError, KeyError):
            return []

    def outputs(self):
        try:
            return {o["name"]: {"width": o["rect"]["width"], "height": o["rect"]["height"], "scale": o.get("scale", 1)}
                    for o in request(GET_OUTPUTS) if o.get("active")}
        except (OSError, EOFError, ValueError, KeyError):
            return super().outputs()

    def focused_output(self):
        try:
            return next((o["name"] for o in request(GET_OUTPUTS) if o.get("focused")), None)
        except (OSError, EOFError, ValueError):
            return None

    def touch_sync(self, output):
        # Sway keeps input config per identifier and applies it when the
        # device appears, so this also covers an engine restart.
        try:
            res = request(RUN_COMMAND, f'input "{IDENTIFIER}" map_to_output {output}')
            ok = all(r.get("success") for r in res)
            self.sync_error = None if ok else "; ".join(r.get("error", "") for r in res)
        except (OSError, EOFError, ValueError) as ex:
            self.sync_error = str(ex)

    def notice(self, output, n_outputs):
        if self.sync_error and n_outputs > 1:
            return f"无法通过 sway IPC 把触摸映射到 {output}：{self.sync_error}", None
        return None
