"""Command line interface, usable by people, scripts and AI agents.

Control commands talk to the running GUI over GApplication's D-Bus action
interface (org.gtk.Actions); profile/point commands edit the JSON files
directly and then ask a running GUI to reload.  No GTK is loaded here.

Exit codes: 0 ok, 1 error, 2 bad usage, 3 key2touch is not running.
"""

import argparse
import json
import sys

from evdev import ecodes as e

from . import profile as prof
from .mapper import MOD_ORDER
from .profile import JOY_DIRS

APP_ID = "io.github.shorin_kiwata.key2touch"
OBJ_PATH = "/" + APP_ID.replace(".", "/")

EXIT_ERR, EXIT_USAGE, EXIT_NOT_RUNNING = 1, 2, 3

MOD_ALIASES = {"ctrl": "CTRL", "control": "CTRL", "alt": "ALT", "shift": "SHIFT",
               "super": "META", "meta": "META", "win": "META", "logo": "META"}
KEY_ALIASES = {"LSHIFT": "LEFTSHIFT", "RSHIFT": "RIGHTSHIFT", "LCTRL": "LEFTCTRL", "RCTRL": "RIGHTCTRL",
               "LALT": "LEFTALT", "RALT": "RIGHTALT", "LSUPER": "LEFTMETA", "RSUPER": "RIGHTMETA",
               "ESCAPE": "ESC", "RETURN": "ENTER", "DEL": "DELETE", "BACKSPACE": "BACKSPACE",
               "PGUP": "PAGEUP", "PGDN": "PAGEDOWN", "CAPS": "CAPSLOCK"}
MOUSE_ALIASES = {"LMB": "BTN_LEFT", "MOUSE1": "BTN_LEFT", "MOUSE_LEFT": "BTN_LEFT", "BTN_LEFT": "BTN_LEFT",
                 "RMB": "BTN_RIGHT", "MOUSE2": "BTN_RIGHT", "MOUSE_RIGHT": "BTN_RIGHT", "BTN_RIGHT": "BTN_RIGHT",
                 "MMB": "BTN_MIDDLE", "MOUSE3": "BTN_MIDDLE", "MOUSE_MIDDLE": "BTN_MIDDLE", "BTN_MIDDLE": "BTN_MIDDLE",
                 "MOUSE4": "BTN_SIDE", "BTN_SIDE": "BTN_SIDE", "MOUSE5": "BTN_EXTRA", "BTN_EXTRA": "BTN_EXTRA"}
_MOUSE_SPEC = {"BTN_LEFT": "lmb", "BTN_RIGHT": "rmb", "BTN_MIDDLE": "mmb", "BTN_SIDE": "mouse4", "BTN_EXTRA": "mouse5"}

COMMANDS = {"on", "off", "toggle", "status", "edit", "show", "quit", "profiles", "profile",
            "point", "set", "window", "outputs", "windows", "check", "agent-help"}

AGENT_HELP = """\
# key2touch for AI agents

key2touch turns keyboard keys into touchscreen touches (for Android games in
Waydroid). A profile is a list of points on one display; each point is a tap
(one key) or a joystick (4 direction keys). Use `--json` on any command.

## First: find the game's display
Points are positioned relative to ONE display (the profile's `output`).
Run `key2touch windows --json` to see which display the game window is on,
then `key2touch set output <name>`. With several monitors this is the step
that, if wrong, shifts every point.

## Coordinates
x, y are fractions of the target display (0..1, origin top-left), or
percentages like `80%`. Measure them on a screenshot of the WHOLE display
(e.g. `grim -o DP-2 shot.png`), not of the game window:
x = pixel_x / image_width, y = pixel_y / image_height. This works at any
scale factor. If the game is not fullscreen, its buttons move whenever the
window moves, so prefer fullscreen (Waydroid: F11).
`size` is a radius as a fraction of the display height (tap default 0.025,
joystick default 0.12).

## Verify
`key2touch check -p NAME --json` reports errors (exit 1) and warnings:
points out of range or overlapping, unbound keys, a display that does not
exist, the game window sitting on a different display than the profile,
missing permissions, compositor setup. Run it after every change.

## Keys
Case-insensitive names: `j`, `space`, `enter`, `esc`, `f1`, `up`, `lshift`,
`kp1` ... Combos join modifiers with `+`: `ctrl+q`, `alt+shift+1`.
Mouse buttons: `lmb`, `rmb`, `mmb`, `mouse4`, `mouse5` (they only act while
the camera area is in use, i.e. when the profile has a `look` point).

## Camera (games like Genshin)
`point add look X Y` adds a camera area: while mapping is active the mouse
is captured and its motion drags a virtual finger from (X, Y); `--sens`
scales it (default 1.0), `--size` is how far the finger may travel before
it is re-centred. Holding the cursor key (default `lalt`) frees the mouse.
Put it on an empty part of the screen, away from buttons.

## Typical session
    key2touch windows --json                 # which display is the game on?
    key2touch profile new genshin            # empty profile
    key2touch set output DP-2 -p genshin
    key2touch point add tap j 85% 78% -p genshin
    key2touch point add stick 15% 70% --keys w,a,s,d --size 0.13 -p genshin
    key2touch point add look 60% 40% --sens 1.2 -p genshin
    key2touch point add tap lmb 85% 78% -p genshin --replace
    key2touch point list -p genshin --json
    key2touch check -p genshin --json        # fix every error it reports
    key2touch set only-focused true -p genshin
    key2touch window add 'waydroid.*' -p genshin
    key2touch profile use genshin            # needs the app running
    key2touch on                             # start mapping
    key2touch status --json

## Commands
    on | off | toggle | status          control the running app (exit 3 if not running)
    show | edit | quit                  show window / open layout editor / quit
    profiles                            list profiles
    profile show|use|new|rm|rename|export|import
    point list|add|set|rm|clear         edit points (index from `point list`)
    set output|hotkey|edit-hotkey|grab|only-focused VALUE   (hotkeys: e.g. ctrl+f12, or none)
    window list|add|rm [PATTERN]        app_id rules for only-focused (* wildcards)
    outputs                             displays: name, logical size, scale, focused
    windows                             open windows: app_id, title, display, rule match
    check                               validate a profile against the live desktop

Exit codes: 0 ok, 1 error, 2 bad usage, 3 not running.
Profiles are JSON files in ~/.config/key2touch/profiles/; `profile export`
and `profile import NAME -` round-trip them through stdout/stdin.
"""


class CliError(Exception):
    def __init__(self, msg, code=EXIT_ERR):
        super().__init__(msg)
        self.code = code


# ---------------------------------------------------------------- running app
def _bus():
    from gi.repository import Gio
    return Gio.bus_get_sync(Gio.BusType.SESSION)


def _call(method, params, reply=None):
    from gi.repository import Gio, GLib
    try:
        return _bus().call_sync(APP_ID, OBJ_PATH, "org.gtk.Actions", method, params,
                                GLib.VariantType(reply) if reply else None,
                                Gio.DBusCallFlags.NO_AUTO_START, 3000, None)
    except GLib.Error as ex:
        if "ServiceUnknown" in ex.message or "not provided by any" in ex.message or "NameHasNoOwner" in ex.message:
            raise CliError("key2touch 没有在运行（先运行 key2touch 或 key2touch --hidden）", EXIT_NOT_RUNNING)
        raise CliError(f"D-Bus 调用失败：{ex.message}")


def running():
    from gi.repository import Gio, GLib
    try:
        res = _bus().call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                               "NameHasOwner", GLib.Variant("(s)", (APP_ID,)), GLib.VariantType("(b)"),
                               Gio.DBusCallFlags.NONE, 1000, None)
        return res.unpack()[0]
    except GLib.Error:
        return False


def activate(action):
    from gi.repository import GLib
    _call("Activate", GLib.Variant("(sava{sv})", (action, [], {})))


def set_state(action, value):
    from gi.repository import GLib
    _call("SetState", GLib.Variant("(sva{sv})", (action, value, {})))


def app_state():
    from gi.repository import GLib
    res = _call("Describe", GLib.Variant("(s)", ("state",)), "((bgav))")
    enabled, sig, state = res.unpack()[0]
    return json.loads(state[0]) if state else {}


def notify_reload(name):
    """Tell a running GUI that profile `name` changed on disk."""
    if running():
        try:
            if app_state().get("profile") == name:
                activate("reload")
        except CliError:
            pass


# ---------------------------------------------------------------- parsing
def parse_key(text):
    """'ctrl+q' -> ('KEY_Q', ['CTRL'])"""
    parts = [p for p in text.replace(" ", "").split("+") if p]
    if not parts:
        raise CliError(f"无效的按键：{text!r}", EXIT_USAGE)
    *mods, key = parts
    groups = []
    for m in mods:
        g = MOD_ALIASES.get(m.lower())
        if not g:
            raise CliError(f"未知的修饰键：{m!r}（可用 ctrl / alt / shift / super）", EXIT_USAGE)
        groups.append(g)
    if key.upper() in MOUSE_ALIASES:
        return MOUSE_ALIASES[key.upper()], [g for g in MOD_ORDER if g in groups]
    k = key.upper().removeprefix("KEY_")
    k = KEY_ALIASES.get(k, k)
    name = f"KEY_{k}"
    if name not in e.ecodes:
        raise CliError(f"未知的按键：{key!r}", EXIT_USAGE)
    return name, [g for g in MOD_ORDER if g in groups]


_REVERSE_ALIASES = {"LEFTSHIFT": "lshift", "RIGHTSHIFT": "rshift", "LEFTCTRL": "lctrl", "RIGHTCTRL": "rctrl",
                    "LEFTALT": "lalt", "RIGHTALT": "ralt", "LEFTMETA": "lsuper", "RIGHTMETA": "rsuper"}
_MOD_SPEC = {"CTRL": "ctrl", "ALT": "alt", "SHIFT": "shift", "META": "super"}


def fmt_key(name, mods=()):
    """Inverse of parse_key: 'KEY_Q', ['CTRL'] -> 'ctrl+q' (round-trips)."""
    if name in _MOUSE_SPEC:
        return "+".join([_MOD_SPEC[m] for m in MOD_ORDER if m in (mods or ())] + [_MOUSE_SPEC[name]])
    k = name.removeprefix("KEY_")
    k = _REVERSE_ALIASES.get(k, k.lower())
    return "+".join([_MOD_SPEC[m] for m in MOD_ORDER if m in (mods or ())] + [k])


def parse_coord(text):
    try:
        v = float(text[:-1]) / 100 if text.endswith("%") else float(text)
    except ValueError:
        raise CliError(f"无效的坐标：{text!r}（用 0..1 的小数或百分比，如 0.8 或 80%）", EXIT_USAGE)
    if not 0 <= v <= 1:
        raise CliError(f"坐标超出范围：{text!r}（应在 0..1 / 0%..100%）", EXIT_USAGE)
    return v


def parse_bool(text):
    t = text.lower()
    if t in ("1", "true", "yes", "on"):
        return True
    if t in ("0", "false", "no", "off"):
        return False
    raise CliError(f"无效的布尔值：{text!r}（用 true / false）", EXIT_USAGE)


def target_profile(args):
    if getattr(args, "profile", None):
        name = args.profile
    elif running():
        name = app_state().get("profile")
    else:
        name = prof.load_settings().get("last_profile")
    names = prof.list_profiles()
    if not name or name not in names:
        if getattr(args, "profile", None):
            raise CliError(f"没有名为「{name}」的配置（key2touch profiles 查看）")
        if not names:
            raise CliError("还没有任何配置，先运行 key2touch profile new <名字>")
        name = names[0]
    return name


def describe_point(i, p):
    if p["type"] == "look":
        return {"index": i, "type": "look", "x": round(p["x"], 4), "y": round(p["y"], 4),
                "size": round(p.get("r", 0.22), 4), "sens": p.get("sens", 1.0),
                "cursor_key": fmt_key(p["cursor_key"]) if p.get("cursor_key") else None}
    if p["type"] == "joystick":
        keys = {d: (fmt_key(p["keys"][d]) if p["keys"].get(d) else None) for d in JOY_DIRS}
        return {"index": i, "type": "joystick", "x": round(p["x"], 4), "y": round(p["y"], 4),
                "size": round(p.get("r", 0.12), 4), "keys": keys}
    return {"index": i, "type": "tap", "x": round(p["x"], 4), "y": round(p["y"], 4),
            "size": round(p.get("r", prof.TAP_R), 4),
            "key": fmt_key(p["key"], p.get("mods")) if p.get("key") else None}


# ---------------------------------------------------------------- output
class Out:
    def __init__(self, as_json):
        self.json = as_json

    def emit(self, data, text):
        if self.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        elif text:
            print(text)


# ---------------------------------------------------------------- commands
def cmd_control(args, out):
    if args.cmd == "status":
        if not running():
            out.emit({"running": False}, "key2touch 没有在运行")
            return EXIT_NOT_RUNNING
        st = app_state()
        st["running"] = True
        for k in ("hotkey", "edit_hotkey"):
            if st.get(k):
                try:
                    st[k] = fmt_key(*parse_key(st[k]))
                except CliError:
                    pass
        state = "编辑中" if st.get("editing") else "生效中" if st.get("active") else \
            "已开启（等待目标窗口）" if st.get("enabled") else "已关闭"
        out.emit(st, f"{state} · 配置 {st.get('profile')} · {st.get('points')} 个按键点 · "
                     f"显示器 {st.get('output')} · 焦点 {st.get('focus') or '-'} · "
                     f"开关热键 {st.get('hotkey') or '-'} · 编辑热键 {st.get('edit_hotkey') or '-'}")
        return 0
    from gi.repository import GLib
    if args.cmd in ("on", "off"):
        set_state("enabled", GLib.Variant("b", args.cmd == "on"))
    elif args.cmd == "toggle":
        activate("enabled")
    else:
        activate(args.cmd)  # show / edit / quit
    if args.cmd in ("on", "off", "toggle"):
        st = app_state()
        out.emit({"enabled": st.get("enabled")}, "映射已" + ("开启" if st.get("enabled") else "关闭"))
    else:
        out.emit({"ok": True}, None)
    return 0


def cmd_profiles(args, out):
    names = prof.list_profiles()
    current = None
    if running():
        try:
            current = app_state().get("profile")
        except CliError:
            pass
    items = [{"name": n, "current": n == current, "points": len(prof.load(n)["points"])} for n in names]
    out.emit(items, "\n".join(f"{'*' if it['current'] else ' '} {it['name']}  ({it['points']} 个按键点)"
                              for it in items) or "（没有配置）")
    return 0


def cmd_profile(args, out):
    sub = args.sub
    if sub == "show":
        name = target_profile(args)
        p = prof.load(name)
        data = {"name": name, "output": p["output"],
                "hotkey": fmt_key(p["toggle_key"], p.get("toggle_mods")) if p.get("toggle_key") else None,
                "edit_hotkey": fmt_key(p["edit_key"], p.get("edit_mods")) if p.get("edit_key") else None,
                "grab": p.get("grab", True),
                "only_focused": p.get("only_focused", False), "windows": p["windows"],
                "points": [describe_point(i, q) for i, q in enumerate(p["points"])]}
        out.emit(data, "\n".join([f"{name}  显示器 {data['output']}  热键 {data['hotkey']}",
                                  *(_point_line(d) for d in data["points"])]))
        return 0
    if sub == "use":
        if not prof.exists(args.name):
            raise CliError(f"没有名为「{args.name}」的配置")
        from gi.repository import GLib
        set_state("profile", GLib.Variant("s", args.name))
        out.emit({"profile": args.name}, f"已切换到「{args.name}」")
        return 0
    if sub == "new":
        _check_new_name(args.name)
        base = prof.load(target_profile(args)) if args.copy and prof.list_profiles() else prof.normalize({})
        if not args.copy:
            base["points"] = []
        prof.save(args.name, base)
        out.emit({"created": args.name}, f"已创建「{args.name}」")
        return 0
    if sub == "rm":
        _need(args.name)
        prof.delete(args.name)
        notify_reload(args.name)
        out.emit({"deleted": args.name}, f"已删除「{args.name}」")
        return 0
    if sub == "rename":
        _need(args.old)
        _check_new_name(args.new)
        prof.rename(args.old, args.new)
        out.emit({"renamed": args.old, "to": args.new}, f"「{args.old}」→「{args.new}」")
        return 0
    if sub == "export":
        name = args.name or target_profile(args)
        _need(name)
        print(json.dumps(prof.load(name), ensure_ascii=False, indent=2))
        return 0
    if sub == "import":
        if not prof.valid_name(args.name):
            raise CliError(f"无效的配置名：{args.name!r}", EXIT_USAGE)
        src = sys.stdin.read() if args.file in (None, "-") else open(args.file, encoding="utf-8").read()
        try:
            data = prof.normalize(json.loads(src))
        except (ValueError, TypeError) as ex:
            raise CliError(f"不是有效的配置 JSON：{ex}")
        for i, p in enumerate(data["points"]):
            if p.get("type") not in ("tap", "joystick") or not (0 <= p.get("x", -1) <= 1 and 0 <= p.get("y", -1) <= 1):
                raise CliError(f"第 {i} 个按键点无效：{p}")
        prof.save(args.name, data)
        notify_reload(args.name)
        out.emit({"imported": args.name, "points": len(data["points"])},
                 f"已导入「{args.name}」（{len(data['points'])} 个按键点）")
        return 0
    raise CliError("缺少子命令", EXIT_USAGE)


def _point_line(d):
    if d["type"] == "look":
        return (f"  [{d['index']}] 视角  ({d['x']:.3f}, {d['y']:.3f})  size {d['size']}  "
                f"sens {d['sens']}  按住 {d['cursor_key']} 显示鼠标")
    if d["type"] == "joystick":
        keys = "/".join(d["keys"][k] or "?" for k in JOY_DIRS)
        return f"  [{d['index']}] 摇杆 {keys}  ({d['x']:.3f}, {d['y']:.3f})  size {d['size']}"
    return f"  [{d['index']}] {d['key'] or '未绑定'}  ({d['x']:.3f}, {d['y']:.3f})  size {d['size']}"


def _need(name):
    if not prof.exists(name):
        raise CliError(f"没有名为「{name}」的配置")


def _check_new_name(name):
    if not prof.valid_name(name):
        raise CliError(f"无效的配置名：{name!r}", EXIT_USAGE)
    if prof.exists(name):
        raise CliError(f"配置「{name}」已存在")


def _edit(args, fn):
    name = target_profile(args)
    p = prof.load(name)
    result = fn(p)
    prof.save(name, p)
    notify_reload(name)
    return name, p, result


def _bind(points, key, mods, keep, replace):
    """A key combo may drive one point only. Without --replace a clash is an
    error (explicit is better for scripts); with it the key moves here and a
    tap left without a key is removed."""
    clash = [i for i, q in enumerate(points) if q is not keep and (
        (q["type"] == "tap" and q.get("key") == key and sorted(q.get("mods") or []) == sorted(mods))
        or (q["type"] == "joystick" and not mods and key in q["keys"].values()))]
    if clash and not replace:
        raise CliError(f"{fmt_key(key, mods)} 已绑定在第 {clash[0]} 个按键点（加 --replace 改绑到这里）")
    _steal(points, key, mods, keep)
    points[:] = [q for q in points if q is keep or q["type"] != "tap" or q.get("key")]


def _steal(points, key, mods, keep):
    for q in points:
        if q is keep:
            continue
        if q["type"] == "tap" and q.get("key") == key and sorted(q.get("mods") or []) == sorted(mods):
            q["key"] = None
        elif q["type"] == "joystick" and not mods:
            for d, k in q["keys"].items():
                if k == key:
                    q["keys"][d] = None


def cmd_point(args, out):
    sub = args.sub
    if sub == "list":
        name = target_profile(args)
        pts = [describe_point(i, q) for i, q in enumerate(prof.load(name)["points"])]
        out.emit(pts, "\n".join(map(_point_line, pts)) or "（没有按键点）")
        return 0
    if sub == "add":
        x, y = parse_coord(args.x), parse_coord(args.y)
        if args.kind == "tap":
            key, mods = parse_key(args.key)
            pt = prof.new_tap(x, y, key, r=args.size or prof.TAP_R)
            pt["mods"] = mods
        elif args.kind == "look":
            pt = prof.new_look(x, y, r=args.size or 0.22)
            if args.sens is not None:
                pt["sens"] = args.sens
            if args.cursor_key:
                pt["cursor_key"] = parse_key(args.cursor_key)[0]
        else:
            keys = [k.strip() for k in (args.keys or "w,a,s,d").split(",")]
            if len(keys) != 4:
                raise CliError("--keys 需要 4 个键，顺序为 上,左,下,右", EXIT_USAGE)
            pt = prof.new_joystick(x, y, r=args.size or 0.12)
            pt["keys"] = {d: parse_key(k)[0] for d, k in zip(JOY_DIRS, keys)}

        def add(p):
            if pt["type"] == "look" and any(q["type"] == "look" for q in p["points"]):
                raise CliError("这个配置已经有视角区域了（每个配置一个；用 point set 修改）")
            p["points"].append(pt)
            if pt["type"] == "look":
                return len(p["points"]) - 1
            if pt["type"] == "tap":
                _bind(p["points"], pt["key"], pt["mods"], pt, args.replace)
            else:
                for k in pt["keys"].values():
                    _bind(p["points"], k, [], pt, args.replace)
            return p["points"].index(pt)

        name, p, i = _edit(args, add)
        d = describe_point(i, pt)
        out.emit(d, f"已添加到「{name}」：" + _point_line(d).strip())
        return 0
    if sub == "set":
        def change(p):
            if not 0 <= args.index < len(p["points"]):
                raise CliError(f"没有第 {args.index} 个按键点（key2touch point list 查看）")
            q = p["points"][args.index]
            if args.x is not None:
                q["x"] = parse_coord(args.x)
            if args.y is not None:
                q["y"] = parse_coord(args.y)
            if args.size is not None:
                q["r"] = args.size
            if args.sens is not None:
                if q["type"] != "look":
                    raise CliError("--sens 只适用于视角区域", EXIT_USAGE)
                q["sens"] = args.sens
            if args.cursor_key is not None:
                if q["type"] != "look":
                    raise CliError("--cursor-key 只适用于视角区域", EXIT_USAGE)
                q["cursor_key"] = parse_key(args.cursor_key)[0]
            if args.key is not None:
                if q["type"] != "tap":
                    raise CliError("摇杆请用 --keys 上,左,下,右", EXIT_USAGE)
                q["key"], q["mods"] = parse_key(args.key)
                _bind(p["points"], q["key"], q["mods"], q, args.replace)
            if args.keys is not None:
                if q["type"] != "joystick":
                    raise CliError("点击类按键点请用 --key", EXIT_USAGE)
                keys = [k.strip() for k in args.keys.split(",")]
                if len(keys) != 4:
                    raise CliError("--keys 需要 4 个键，顺序为 上,左,下,右", EXIT_USAGE)
                q["keys"] = {d: parse_key(k)[0] for d, k in zip(JOY_DIRS, keys)}
                for k in q["keys"].values():
                    _bind(p["points"], k, [], q, args.replace)
            return q

        name, p, q = _edit(args, change)
        d = describe_point(p["points"].index(q), q)
        out.emit(d, "已修改：" + _point_line(d).strip())
        return 0
    if sub == "rm":
        def rm(p):
            n = len(p["points"])
            bad = [i for i in args.index if not 0 <= i < n]
            if bad:
                raise CliError(f"没有这些按键点：{bad}")
            for i in sorted(set(args.index), reverse=True):
                del p["points"][i]
            return len(p["points"])

        name, p, left = _edit(args, rm)
        out.emit({"removed": sorted(set(args.index)), "points": left}, f"已删除，剩余 {left} 个按键点")
        return 0
    if sub == "clear":
        name, p, _ = _edit(args, lambda p: p["points"].clear())
        out.emit({"cleared": name}, f"已清空「{name}」")
        return 0
    raise CliError("缺少子命令", EXIT_USAGE)


def cmd_set(args, out):
    if args.option == "output" and not args.force:
        outs = _backend().outputs()
        if outs and args.value not in outs:
            raise CliError(f"没有名为 {args.value} 的显示器（可用：{', '.join(sorted(outs))}；"
                           "确定要用就加 --force）")

    def change(p):
        if args.option == "output":
            p["output"] = args.value
        elif args.option in ("hotkey", "edit-hotkey"):
            kind = "toggle" if args.option == "hotkey" else "edit"
            if args.value.lower() in ("none", ""):
                p[f"{kind}_key"], p[f"{kind}_mods"] = None, []
            else:
                key, mods = parse_key(args.value)
                other = "edit" if kind == "toggle" else "toggle"
                if key == p.get(f"{other}_key") and mods == (p.get(f"{other}_mods") or []):
                    raise CliError(f"{fmt_key(key, mods)} 已经被另一个热键使用")
                p[f"{kind}_key"], p[f"{kind}_mods"] = key, mods
        elif args.option == "grab":
            p["grab"] = parse_bool(args.value)
        elif args.option == "only-focused":
            p["only_focused"] = parse_bool(args.value)

    name, p, _ = _edit(args, change)
    out.emit({"profile": name, args.option: args.value}, f"「{name}」：{args.option} = {args.value}")
    return 0


def cmd_window(args, out):
    if args.sub == "list":
        name = target_profile(args)
        rules = prof.load(name)["windows"]
        out.emit(rules, "\n".join(rules) or "（没有规则）")
        return 0
    if not args.pattern:
        raise CliError("需要窗口 app_id 规则，例如 'waydroid.*'", EXIT_USAGE)

    def change(p):
        if args.sub == "add" and args.pattern not in p["windows"]:
            p["windows"].append(args.pattern)
        elif args.sub == "rm":
            if args.pattern not in p["windows"]:
                raise CliError(f"没有这条规则：{args.pattern}")
            p["windows"].remove(args.pattern)

    name, p, _ = _edit(args, change)
    out.emit(p["windows"], "\n".join(p["windows"]) or "（没有规则）")
    return 0


# ---------------------------------------------------------------- desktop queries
def _backend():
    from . import desktop
    return desktop.detect()


def cmd_outputs(args, out):
    b = _backend()
    focused = b.focused_output()
    items = [{"name": n, **geo, "focused": n == focused} for n, geo in sorted(b.outputs().items())]
    out.emit(items, "\n".join(
        f"{'*' if it['focused'] else ' '} {it['name']}" +
        (f"  {it['width']}×{it['height']} @{it['scale']:g}x" if "width" in it else "") for it in items))
    return 0


def _rules(args):
    try:
        return prof.load(target_profile(args))["windows"]
    except CliError:
        return []


def cmd_windows(args, out):
    from .engine import window_matches
    b = _backend()
    rules = _rules(args)
    items = [{**w, "matches_rules": window_matches(w["app_id"], rules)} for w in b.windows()]
    out.emit(items, "\n".join(
        f"{'*' if w.get('focused') else ' '} {w['app_id']}  [{w.get('output') or '?'}]"
        f"{'  ← 匹配规则' if w['matches_rules'] else ''}  {w['title'][:40]}" for w in items)
        or f"（{b.label} 无法列出窗口）")
    return 0


def check_profile(name, p, b):
    """-> list of {"level": "error"|"warning", "code", "message", ...}"""
    import os
    from .engine import window_matches
    issues = []

    def add(level, code, message, **extra):
        issues.append({"level": level, "code": code, "message": message, **extra})

    outs = b.outputs()
    geo = outs.get(p["output"]) or {}
    if not p["output"]:
        add("error", "no_output", "没有设置显示器（key2touch set output <名字>）")
    elif outs and p["output"] not in outs:
        add("error", "output_missing", f"显示器 {p['output']} 不存在（可用：{', '.join(sorted(outs))}）")
    aspect = geo["width"] / geo["height"] if geo.get("width") else 16 / 9

    windows = b.windows()
    matching = [w for w in windows if window_matches(w["app_id"], p["windows"])]
    for w in matching:
        if w.get("output") and p["output"] and w["output"] != p["output"]:
            add("error", "window_on_other_output",
                f"匹配的窗口 {w['app_id']} 在 {w['output']} 上，但配置的显示器是 {p['output']}",
                app_id=w["app_id"], window_output=w["output"])
    if p.get("only_focused"):
        if not b.supports_focus:
            add("warning", "no_focus_tracking", f"{b.label} 无法获取窗口焦点，「仅聚焦指定窗口时生效」不起作用")
        elif not p["windows"]:
            add("warning", "no_window_rules", "开启了仅聚焦指定窗口时生效，但没有窗口规则（等于不限制）")
        elif windows and not matching:
            add("warning", "no_matching_window", "没有打开的窗口匹配窗口规则（游戏还没开？）")

    pts = p["points"]
    if any(q["type"] == "look" for q in pts) and not (p.get("only_focused") and b.supports_focus):
        add("warning", "look_without_focus",
            "有视角区域时鼠标会被占用：建议开启仅聚焦指定窗口时生效（set only-focused true），"
            "否则切到别的窗口鼠标仍被占用，只能按住显示鼠标的键或用开关热键")
    if not pts:
        add("warning", "no_points", "没有按键点")
    for i, q in enumerate(pts):
        r = q.get("r", prof.TAP_R if q["type"] == "tap" else 0.12)
        if not (0 <= q["x"] <= 1 and 0 <= q["y"] <= 1):
            add("error", "out_of_range", f"第 {i} 个按键点坐标越界：({q['x']}, {q['y']})", point=i)
            continue
        rx = r / aspect
        if q["x"] - rx < 0 or q["x"] + rx > 1 or q["y"] - r < 0 or q["y"] + r > 1:
            add("warning", "partly_offscreen", f"第 {i} 个按键点的范围超出了屏幕边缘", point=i)
        if q["type"] == "look":
            continue
        if q["type"] == "tap" and not q.get("key"):
            add("warning", "unbound", f"第 {i} 个按键点没有绑定按键", point=i)
        if q["type"] == "joystick" and not all(q["keys"].get(d) for d in JOY_DIRS):
            add("warning", "unbound", f"第 {i} 个摇杆有方向没有绑定按键", point=i)
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            a, c = pts[i], pts[j]
            ra = a.get("r", prof.TAP_R if a["type"] == "tap" else 0.12)
            rc = c.get("r", prof.TAP_R if c["type"] == "tap" else 0.12)
            dist = (((a["x"] - c["x"]) * aspect) ** 2 + (a["y"] - c["y"]) ** 2) ** 0.5
            if dist < ra + rc:
                kind = "inside" if dist < max(ra, rc) else "overlap"
                if "look" in (a["type"], c["type"]):
                    continue  # buttons inside the camera area are normal; touches don't collide
                add("warning", f"points_{kind}",
                    f"第 {i} 和第 {j} 个按键点" + ("一个的中心落在另一个范围内" if kind == "inside" else "范围重叠"),
                    points=[i, j])

    if not os.access("/dev/uinput", os.W_OK):
        add("error", "uinput_denied", "没有 /dev/uinput 写权限（安装包会带 udev 规则；或检查 uinput 模块）")
    events = glob_events()
    if events and not any(os.access(ev, os.R_OK) for ev in events):
        add("error", "input_denied", "读不了 /dev/input/event*：把用户加入 input 组后重新登录")
    notice = b.notice(p["output"], len(outs))
    if notice:
        add("warning", "desktop_setup", notice[0])
    return issues


def glob_events():
    import glob
    return glob.glob("/dev/input/event*")


def cmd_check(args, out):
    name = target_profile(args)
    p = prof.load(name)
    issues = check_profile(name, p, _backend())
    errors = [x for x in issues if x["level"] == "error"]
    data = {"profile": name, "ok": not errors, "errors": errors,
            "warnings": [x for x in issues if x["level"] == "warning"]}
    lines = [f"{'✗' if x['level'] == 'error' else '!'} {x['message']}" for x in issues]
    out.emit(data, "\n".join(lines) if lines else f"✓ 「{name}」没有发现问题")
    return EXIT_ERR if errors else 0


# ---------------------------------------------------------------- argparse
def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="输出 JSON（给脚本 / AI agent）")
    target = argparse.ArgumentParser(add_help=False)
    target.add_argument("-p", "--profile", help="要操作的配置（默认：正在使用的配置）")

    ap = argparse.ArgumentParser(prog="key2touch", description="将键盘按键映射为触屏操作。不带命令时启动图形界面。",
                                 epilog="给 AI agent 的说明：key2touch agent-help", parents=[common])
    ap.add_argument("--hidden", action="store_true", help="启动图形界面时只显示托盘图标")
    ap.add_argument("-p", "--profile", help="启动图形界面时打开的配置")
    sp = ap.add_subparsers(dest="cmd", metavar="命令")
    for name, help_ in (("on", "开启映射"), ("off", "关闭映射"), ("toggle", "开关映射"),
                        ("status", "查看状态"), ("show", "显示主窗口"), ("edit", "打开布局编辑器"),
                        ("quit", "退出正在运行的 key2touch")):
        sp.add_parser(name, help=help_, parents=[common])
    sp.add_parser("profiles", help="列出所有配置", parents=[common])
    sp.add_parser("agent-help", help="打印给 AI agent 的使用说明", parents=[common])

    pp = sp.add_parser("profile", help="管理配置", parents=[common]).add_subparsers(dest="sub", metavar="子命令")
    pp.add_parser("show", help="查看配置", parents=[common, target])
    p = pp.add_parser("use", help="让正在运行的 key2touch 切换到此配置", parents=[common])
    p.add_argument("name")
    p = pp.add_parser("new", help="新建空配置", parents=[common, target])
    p.add_argument("name")
    p.add_argument("--copy", action="store_true", help="复制 -p 指定（或当前）的配置")
    p = pp.add_parser("rm", help="删除配置", parents=[common])
    p.add_argument("name")
    p = pp.add_parser("rename", help="重命名配置", parents=[common])
    p.add_argument("old")
    p.add_argument("new")
    p = pp.add_parser("export", help="以 JSON 输出配置", parents=[common])
    p.add_argument("name", nargs="?")
    p = pp.add_parser("import", help="从 JSON 文件或标准输入导入配置", parents=[common])
    p.add_argument("name")
    p.add_argument("file", nargs="?", help="JSON 文件；省略或 - 表示标准输入")

    pt = sp.add_parser("point", help="编辑按键点", parents=[common]).add_subparsers(dest="sub", metavar="子命令")
    pt.add_parser("list", help="列出按键点", parents=[common, target])
    add = pt.add_parser("add", help="添加按键点", parents=[common]).add_subparsers(dest="kind", metavar="类型")
    a = add.add_parser("tap", help="点击：KEY X Y", parents=[common, target])
    a.add_argument("key", help="按键，如 j、space、ctrl+q")
    a.add_argument("x", help="横坐标，0..1 或百分比")
    a.add_argument("y", help="纵坐标，0..1 或百分比")
    a.add_argument("--size", type=float, help="半径，占显示器高度的比例（默认 0.025）")
    a.add_argument("--replace", action="store_true", help="按键已被别的按键点使用时，改绑到这里")
    a = add.add_parser("look", help="视角区域（鼠标拖动视角）：X Y", parents=[common, target])
    a.add_argument("x")
    a.add_argument("y")
    a.add_argument("--size", type=float, help="活动范围半径，占显示器高度的比例（默认 0.22）")
    a.add_argument("--sens", type=float, help="灵敏度（默认 1.0）")
    a.add_argument("--cursor-key", help="按住显示鼠标的键（默认 lalt）")
    a = add.add_parser("stick", help="摇杆：X Y", parents=[common, target])
    a.add_argument("x")
    a.add_argument("y")
    a.add_argument("--keys", help="上,左,下,右（默认 w,a,s,d）")
    a.add_argument("--size", type=float, help="半径，占显示器高度的比例（默认 0.12）")
    a.add_argument("--replace", action="store_true", help="按键已被别的按键点使用时，改绑到这里")
    p = pt.add_parser("set", help="修改按键点", parents=[common, target])
    p.add_argument("index", type=int)
    p.add_argument("--x")
    p.add_argument("--y")
    p.add_argument("--size", type=float)
    p.add_argument("--key", help="点击类按键点的按键")
    p.add_argument("--keys", help="摇杆的 上,左,下,右")
    p.add_argument("--sens", type=float, help="视角灵敏度")
    p.add_argument("--cursor-key", help="视角：按住显示鼠标的键")
    p.add_argument("--replace", action="store_true", help="按键已被别的按键点使用时，改绑到这里")
    p = pt.add_parser("rm", help="删除按键点", parents=[common, target])
    p.add_argument("index", type=int, nargs="+")
    pt.add_parser("clear", help="清空按键点", parents=[common, target])

    p = sp.add_parser("set", help="修改配置选项", parents=[common, target])
    p.add_argument("option", choices=["output", "hotkey", "edit-hotkey", "grab", "only-focused"])
    p.add_argument("value")
    p.add_argument("--force", action="store_true", help="output：不检查显示器是否存在")

    sp.add_parser("outputs", help="列出显示器", parents=[common])
    sp.add_parser("windows", help="列出窗口及其所在显示器", parents=[common, target])
    sp.add_parser("check", help="自检配置（越界、重叠、显示器、权限…）", parents=[common, target])

    p = sp.add_parser("window", help="仅聚焦指定窗口时生效的匹配规则", parents=[common, target])
    p.add_argument("sub", choices=["list", "add", "rm"])
    p.add_argument("pattern", nargs="?")
    return ap


def main(argv):
    ap = build_parser()
    args = ap.parse_args(argv)
    out = Out(getattr(args, "json", False))
    try:
        if args.cmd == "agent-help":
            print(AGENT_HELP, end="")
            return 0
        if args.cmd in ("on", "off", "toggle", "status", "show", "edit", "quit"):
            return cmd_control(args, out)
        if args.cmd == "profiles":
            return cmd_profiles(args, out)
        handler = {"profile": cmd_profile, "point": cmd_point, "set": cmd_set, "window": cmd_window,
                   "outputs": cmd_outputs, "windows": cmd_windows, "check": cmd_check}[args.cmd]
        if args.cmd in ("profile", "point") and not getattr(args, "sub", None):
            ap.parse_args([args.cmd, "--help"])
        if args.cmd == "point" and args.sub == "add" and not args.kind:
            ap.parse_args(["point", "add", "--help"])
        return handler(args, out)
    except CliError as ex:
        if out.json:
            print(json.dumps({"error": str(ex), "code": ex.code}, ensure_ascii=False), file=sys.stderr)
        else:
            print(f"key2touch: {ex}", file=sys.stderr)
        return ex.code
