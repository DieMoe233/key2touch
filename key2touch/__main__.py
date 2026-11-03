"""python -m key2touch [--hidden] [-p PROFILE] | key2touch <command> (see --help)"""

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


def main():
    from .cli import build_parser, main as cli_main
    args = build_parser().parse_args(sys.argv[1:])  # before re-exec: --help / errors need no GTK
    if args.cmd:
        sys.exit(cli_main(sys.argv[1:]))
    ensure_layer_shell_preload()
    from .gui import Key2Touch
    sys.exit(Key2Touch(args.profile, hidden=args.hidden).run([sys.argv[0]]))


if __name__ == "__main__":
    main()
