"""python -m key2touch [profile-name]"""

import ctypes.util
import os
import sys


def ensure_layer_shell_preload():
    # gtk4-layer-shell must be loaded before libwayland-client, otherwise it
    # cannot intercept the protocol and layer surfaces fail to initialize.
    if "gtk4-layer-shell" in os.environ.get("LD_PRELOAD", ""):
        return
    lib = ctypes.util.find_library("gtk4-layer-shell")
    if not lib:
        sys.exit("找不到 libgtk4-layer-shell，请安装 gtk4-layer-shell")
    for d in ("/usr/lib", "/usr/lib64", "/usr/local/lib"):
        if os.path.exists(os.path.join(d, lib)):
            lib = os.path.join(d, lib)
            break
    env = dict(os.environ)
    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = ":".join(filter(None, [pkg_root, env.get("PYTHONPATH")]))
    if os.access(pkg_root, os.W_OK):
        # Running from a source checkout: keep bytecode out of the tree.
        # (A packaged install ships precompiled bytecode instead.)
        env.setdefault("PYTHONPYCACHEPREFIX", os.path.join(
            os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "key2touch", "pycache"))
    env["LD_PRELOAD"] = ":".join(filter(None, [lib, env.get("LD_PRELOAD")]))
    os.execve(sys.executable, [sys.executable, "-m", "key2touch", *sys.argv[1:]], env)


def parse_args():
    import argparse
    ap = argparse.ArgumentParser(prog="python -m key2touch",
                                 description="键盘 → 虚拟触摸屏映射（niri / Waydroid）")
    ap.add_argument("--hidden", action="store_true", help="启动时只显示托盘图标，不打开窗口")
    ap.add_argument("profile", nargs="?", help="要打开的配置名（~/.config/key2touch/profiles/<名字>.json）")
    return ap.parse_args()


def main():
    args = parse_args()  # before re-exec, so --help never needs GTK
    ensure_layer_shell_preload()
    from .gui import Key2Touch
    sys.exit(Key2Touch(args.profile, hidden=args.hidden).run([sys.argv[0]]))


if __name__ == "__main__":
    main()
