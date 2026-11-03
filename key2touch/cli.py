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

COMMANDS = {"on", "off", "toggle", "status", "edit", "show", "quit", "profiles", "profile",
            "point", "set", "window", "agent-help"}

AGENT_HELP = """\
# key2touch for AI agents

key2touch turns keyboard keys into touchscreen touches (for Android games in
Waydroid). A profile is a list of points on one display; each point is a tap
(one key) or a joystick (4 direction keys). Use `--json` on any command.

## Coordinates
x, y are fractions of the target display (0..1, origin top-left), or
percentages like `80%`. To place a point on a game button seen in a screenshot
of the whole display: x = pixel_x / width, y = pixel_y / height.
`size` is a radius as a fraction of the display height (tap default 0.025,
joystick default 0.12).

## Keys
Case-insensitive names: `j`, `space`, `enter`, `esc`, `f1`, `up`, `lshift`,
`kp1` ... Combos join modifiers with `+`: `ctrl+q`, `alt+shift+1`.

## Typical session
    key2touch profile new genshin            # empty profile
    key2touch point add tap j 85% 78% -p genshin
    key2touch point add stick 15% 70% --keys w,a,s,d --size 0.13 -p genshin
    key2touch point list -p genshin --json
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
    set output|hotkey|grab|only-focused VALUE
    window list|add|rm [PATTERN]        app_id rules for only-focused (* wildcards)

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
        if st.get("hotkey"):
            try:
                st["hotkey"] = fmt_key(*parse_key(st["hotkey"]))
            except CliError:
                pass
        state = "编辑中" if st.get("editing") else "生效中" if st.get("active") else \
            "已开启（等待目标窗口）" if st.get("enabled") else "已关闭"
        out.emit(st, f"{state} · 配置 {st.get('profile')} · {st.get('points')} 个按键点 · "
                     f"显示器 {st.get('output')} · 焦点 {st.get('focus') or '-'} · 热键 {st.get('hotkey') or '-'}")
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
        data = {"name": name, "output": p["output"], "hotkey": fmt_key(p["toggle_key"], p.get("toggle_mods"))
                if p.get("toggle_key") else None, "grab": p.get("grab", True),
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
        else:
            keys = [k.strip() for k in (args.keys or "w,a,s,d").split(",")]
            if len(keys) != 4:
                raise CliError("--keys 需要 4 个键，顺序为 上,左,下,右", EXIT_USAGE)
            pt = prof.new_joystick(x, y, r=args.size or 0.12)
            pt["keys"] = {d: parse_key(k)[0] for d, k in zip(JOY_DIRS, keys)}

        def add(p):
            p["points"].append(pt)
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
    def change(p):
        if args.option == "output":
            p["output"] = args.value
        elif args.option == "hotkey":
            if args.value.lower() in ("none", ""):
                p["toggle_key"], p["toggle_mods"] = None, []
            else:
                p["toggle_key"], p["toggle_mods"] = parse_key(args.value)
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
    p.add_argument("--replace", action="store_true", help="按键已被别的按键点使用时，改绑到这里")
    p = pt.add_parser("rm", help="删除按键点", parents=[common, target])
    p.add_argument("index", type=int, nargs="+")
    pt.add_parser("clear", help="清空按键点", parents=[common, target])

    p = sp.add_parser("set", help="修改配置选项", parents=[common, target])
    p.add_argument("option", choices=["output", "hotkey", "grab", "only-focused"])
    p.add_argument("value")

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
        handler = {"profile": cmd_profile, "point": cmd_point, "set": cmd_set, "window": cmd_window}[args.cmd]
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
