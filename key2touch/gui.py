"""Control window (libadwaita): profiles, target, options; owns engine + HUD."""

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import desktop, profile as prof  # noqa: E402
from .mapper import MOD_ORDER  # noqa: E402
from .overlay import MOD_LABEL, MOD_NAME_GROUP, LS, Overlay, combo_label, key_name  # noqa: E402

CSS = b"""
window.key2touch-overlay, window.key2touch-overlay > * { background: transparent; }
.key2touch-bar, .key2touch-panel {
  background: alpha(@window_bg_color, 0.94);
  border-radius: 14px; padding: 8px;
  box-shadow: 0 4px 18px rgba(0,0,0,0.35);
}
.key2touch-panel { padding: 16px; }
.key2touch-toast {
  background: alpha(@window_bg_color, 0.94); border-radius: 99px; padding: 6px 16px;
}
"""


class EngineProc:
    def __init__(self, on_msg):
        env = dict(os.environ)
        env.pop("LD_PRELOAD", None)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        prof.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.log = open(prof.CACHE_DIR / "engine.log", "ab")
        self.p = subprocess.Popen([sys.executable, "-m", "key2touch.engine"], env=env,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log)
        self.on_msg = on_msg
        self.buf = b""
        GLib.io_add_watch(self.p.stdout.fileno(), GLib.PRIORITY_HIGH,
                          GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self._read)

    def _read(self, fd, cond):
        data = os.read(fd, 65536)
        if not data:
            self.on_msg({"ev": "exit", "code": self.p.poll()})
            return False
        self.buf += data
        *lines, self.buf = self.buf.split(b"\n")
        for line in lines:
            try:
                self.on_msg(json.loads(line))
            except ValueError:
                pass
        return True

    def send(self, **msg):
        try:
            self.p.stdin.write((json.dumps(msg) + "\n").encode())
            self.p.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass

    def stop(self):
        self.send(cmd="quit")
        try:
            self.p.wait(2)
        except subprocess.TimeoutExpired:
            self.p.kill()


def gdk_outputs():
    """connector -> logical geometry. GDK reports the same logical rectangle
    the compositor maps touch onto and the layer surface fills, on any desktop."""
    out = {}
    mons = Gdk.Display.get_default().get_monitors()
    for i in range(mons.get_n_items()):
        m = mons.get_item(i)
        name = m.get_connector()
        if not name:
            continue
        g = m.get_geometry()
        out[name] = {"x": g.x, "y": g.y, "width": g.width, "height": g.height, "scale": m.get_scale()}
    return out


class Key2Touch(Adw.Application):
    def __init__(self, initial=None):
        super().__init__(application_id="io.github.shorin_kiwata.key2touch")
        self.initial = initial
        self.engine = None
        self.hud = None
        self.editor = None
        self.engine_state = {"active": False, "enabled": True}
        self._syncing = False
        self.capturing_hotkey = False
        self.backend = desktop.detect()
        self.layer_shell = LS.is_supported()
        self.synced_after_start = False
        self.hk_mods = []  # modifier key names held while capturing the hotkey
        self.hk_solo = False

    # ================================================================ model
    def profile_names(self):
        return prof.list_profiles()

    def create_profile(self, name, empty=True):
        if not prof.valid_name(name) or prof.exists(name):
            return False
        p = copy.deepcopy(self.profile)
        if empty:
            p["points"] = []
        prof.save(name, p)
        self.reload_profile_list()
        return True

    def save_current(self):
        prof.save(self.name, self.profile)

    def switch_profile(self, name):
        self.name = name
        self.profile = prof.load(name)
        if self.profile["output"] not in self.outputs:
            self.profile["output"] = self.default_output()
        self.settings["last_profile"] = name
        prof.save_settings(self.settings)
        self.load_into_ui()

    def default_output(self):
        o = self.backend.focused_output()
        return o if o in self.outputs else self.out_names[0]

    def apply(self):
        """Persist the profile and push it to the engine / compositor / HUD."""
        self.save_current()
        out = self.outputs[self.profile["output"]]
        self.backend.touch_sync(self.profile["output"])
        self.engine.send(cmd="profile", profile=self.profile, aspect=out["width"] / out["height"])
        self.refresh_touch_row()
        self.refresh_status()
        self.rebuild_hud()

    def set_opt(self, k, v):
        if self._syncing:
            return
        self.profile[k] = v
        self.apply()

    # ================================================================ UI
    def do_activate(self):
        if self.get_windows():
            self.get_windows()[0].present()
            return
        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.PREFER_DARK)
        self.outputs = gdk_outputs()
        self.out_names = sorted(self.outputs)
        migrated = prof.migrate_legacy()
        self.settings = prof.load_settings()

        names = prof.list_profiles()
        name = self.initial or self.settings.get("last_profile")
        if not name or not prof.valid_name(name) or (name not in names and not self.initial):
            name = names[0] if names else "默认"
        self.name = name
        self.profile = prof.load(name)
        if self.profile["output"] not in self.outputs:
            self.profile["output"] = self.default_output()
        prof.save(name, self.profile)

        self.win = Adw.ApplicationWindow(application=self, title="key2touch",
                                         default_width=560, default_height=780)
        self.win.connect("close-request", lambda *_: self.quit() or False)
        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        view.add_top_bar(self._build_header())
        page = Adw.PreferencesPage()
        for g in (self._group_status(), self._group_target(), self._group_keyboard(),
                  self._group_hud(), self._group_files()):
            page.add(g)
        view.set_content(page)
        self.toasts.set_child(view)
        self.win.set_content(self.toasts)

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_window_key)
        keys.connect("key-released", self._on_window_key_released)
        self.win.add_controller(keys)

        self._setup_actions()
        self.engine = EngineProc(self.on_engine)
        self.load_into_ui()
        self.win.present()
        if migrated:
            self.toast("已从 ~/.config/waymap 迁移配置")

    def do_shutdown(self):
        if self.engine:
            self.engine.stop()
        Adw.Application.do_shutdown(self)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=2))

    def _build_header(self):
        hb = Adw.HeaderBar()
        self.prof_model = Gtk.StringList.new(prof.list_profiles())
        self.prof_dd = Gtk.DropDown(model=self.prof_model, tooltip_text="切换配置")
        self.prof_dd.connect("notify::selected", self._on_profile_dd)
        hb.set_title_widget(self.prof_dd)

        menu = Gio.Menu()
        menu.append("新建配置…", "app.new-profile")
        menu.append("复制当前配置…", "app.dup-profile")
        menu.append("重命名…", "app.rename-profile")
        menu.append("删除当前配置…", "app.delete-profile")
        sec = Gio.Menu()
        sec.append("打开配置文件夹", "app.open-config")
        menu.append_section(None, sec)
        hb.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="配置管理"))
        return hb

    def _group_status(self):
        g = Adw.PreferencesGroup()
        self.status_row = Adw.ActionRow(title="状态")
        self.status_icon = Gtk.Image(icon_name="media-playback-pause-symbolic")
        self.status_row.add_prefix(self.status_icon)
        self.enable_sw = Gtk.Switch(valign=Gtk.Align.CENTER, active=True, tooltip_text="映射开关")
        self.enable_sw.connect("notify::active", self._on_enable)
        self.status_row.add_suffix(self.enable_sw)
        self.status_row.set_activatable_widget(self.enable_sw)
        g.add(self.status_row)

        edit = Adw.ButtonRow(title="编辑布局", start_icon_name="document-edit-symbolic")
        edit.add_css_class("suggested-action")
        edit.connect("activated", self.on_edit)
        g.add(edit)
        return g

    def _group_target(self):
        g = Adw.PreferencesGroup(title="目标")
        labels = [f"{n}  ·  {o['width']}×{o['height']}  @{o['scale']:g}x"
                  for n, o in ((n, self.outputs[n]) for n in self.out_names)]
        self.out_row = Adw.ComboRow(title="输出", subtitle="游戏窗口所在的屏幕",
                                    model=Gtk.StringList.new(labels))
        self.out_row.connect("notify::selected", self._on_output)
        g.add(self.out_row)

        self.touch_row = Adw.ActionRow(title="触摸映射")
        self.touch_icon = Gtk.Image()
        self.touch_row.add_prefix(self.touch_icon)
        self.touch_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        self.touch_btn.add_css_class("suggested-action")
        self.touch_btn.connect("clicked", self._on_touch_btn)
        self.touch_row.add_suffix(self.touch_btn)
        g.add(self.touch_row)

        focus_sub = ("焦点在其他窗口时键盘照常使用" if self.backend.supports_focus
                     else f"当前桌面（{self.backend.label}）无法获取窗口焦点，请用开关热键手动启停")
        self.focus_row = Adw.ExpanderRow(title="仅在指定窗口获得焦点时生效",
                                         subtitle=focus_sub, show_enable_switch=True,
                                         sensitive=self.backend.supports_focus)
        self.focus_row.connect("notify::enable-expansion",
                               lambda r, _: self.set_opt("auto_focus", r.get_enable_expansion()))
        self.app_row = Adw.EntryRow(title="窗口 app_id（正则）", show_apply_button=True)
        self.app_row.connect("apply", lambda r: self.set_opt("app_id", r.get_text()))
        pick = Gtk.MenuButton(icon_name="view-list-symbolic", valign=Gtk.Align.CENTER,
                              tooltip_text="从当前打开的窗口选择")
        pick.add_css_class("flat")
        pick.set_create_popup_func(self._fill_window_picker)
        self.app_row.add_suffix(pick)
        self.focus_row.add_row(self.app_row)
        g.add(self.focus_row)
        return g

    def _group_keyboard(self):
        g = Adw.PreferencesGroup(title="键盘")
        self.grab_row = Adw.SwitchRow(title="独占键盘",
                                      subtitle="生效时已映射的键只变成触摸，不再传给应用")
        self.grab_row.connect("notify::active", lambda r, _: self.set_opt("grab", r.get_active()))
        g.add(self.grab_row)
        self.hotkey_row = Adw.ActionRow(title="开关热键", subtitle="随时启停映射（全局有效）")
        self.hotkey_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        self.hotkey_btn.connect("clicked", self._on_hotkey_btn)
        self.hotkey_row.add_suffix(self.hotkey_btn)
        g.add(self.hotkey_row)
        return g

    def _group_hud(self):
        g = Adw.PreferencesGroup(title="HUD")
        self.hud_row = Adw.SwitchRow(
            title="显示 HUD",
            subtitle=("在屏幕上显示按键位置，按下时高亮" if self.layer_shell
                      else "当前桌面不支持 wlr-layer-shell，HUD 不可用"),
            active=self.settings.get("hud", True), sensitive=self.layer_shell)
        self.hud_row.connect("notify::active", self._on_hud_setting)
        g.add(self.hud_row)
        row = Adw.ActionRow(title="不透明度")
        self.opacity = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.15, 1.0, 0.05)
        self.opacity.set_value(self.settings.get("hud_opacity", 0.6))
        self.opacity.set_draw_value(False)
        self.opacity.set_hexpand(True)
        self.opacity.set_size_request(200, -1)
        self.opacity.connect("value-changed", self._on_hud_setting)
        row.add_suffix(self.opacity)
        g.add(row)
        return g

    def _group_files(self):
        g = Adw.PreferencesGroup(title="文件")
        self.file_row = Adw.ActionRow(title="当前配置文件", subtitle_selectable=True)
        btn = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, tooltip_text="打开文件夹")
        btn.add_css_class("flat")
        btn.connect("clicked", lambda *_: self.activate_action("open-config"))
        self.file_row.add_suffix(btn)
        g.add(self.file_row)
        g.add(Adw.ActionRow(title="缓存与日志", subtitle=str(prof.CACHE_DIR), subtitle_selectable=True))
        return g

    def _setup_actions(self):
        for name, cb in (("new-profile", lambda *_: self._ask_name("新建配置", "", self._do_new)),
                         ("dup-profile", lambda *_: self._ask_name("复制配置", f"{self.name} 副本", self._do_dup)),
                         ("rename-profile", lambda *_: self._ask_name("重命名配置", self.name, self._do_rename)),
                         ("delete-profile", self._ask_delete),
                         ("open-config", lambda *_: Gio.AppInfo.launch_default_for_uri(
                             prof.PROFILE_DIR.as_uri(), None))):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            self.add_action(a)

    # ---- profile dialogs ----
    def _ask_name(self, heading, initial, done):
        dlg = Adw.AlertDialog(heading=heading)
        entry = Gtk.Entry(text=initial, activates_default=True)
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "取消")
        dlg.add_response("ok", "确定")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("ok")
        dlg.connect("response", lambda d, r: r == "ok" and done(entry.get_text().strip()))
        dlg.present(self.win)

    def _do_new(self, name):
        if self.create_profile(name, empty=True):
            self.switch_profile(name)
        else:
            self.toast("名称无效或已存在")

    def _do_dup(self, name):
        self.save_current()
        if self.create_profile(name, empty=False):
            self.switch_profile(name)
        else:
            self.toast("名称无效或已存在")

    def _do_rename(self, name):
        if name == self.name:
            return
        if not prof.valid_name(name) or prof.exists(name):
            self.toast("名称无效或已存在")
            return
        self.save_current()
        prof.rename(self.name, name)
        self.switch_profile(name)

    def _ask_delete(self, *_):
        dlg = Adw.AlertDialog(heading=f"删除配置「{self.name}」？", body="配置文件会被删除，无法恢复。")
        dlg.add_response("cancel", "取消")
        dlg.add_response("delete", "删除")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def resp(d, r):
            if r != "delete":
                return
            prof.delete(self.name)
            rest = prof.list_profiles()
            if not rest:
                prof.save("默认", prof.normalize({"output": self.profile["output"]}))
                rest = ["默认"]
            self.switch_profile(rest[0])

        dlg.connect("response", resp)
        dlg.present(self.win)

    def reload_profile_list(self):
        was = self._syncing
        self._syncing = True
        names = prof.list_profiles()
        self.prof_model.splice(0, self.prof_model.get_n_items(), names)
        if self.name in names:
            self.prof_dd.set_selected(names.index(self.name))
        self._syncing = was

    def _on_profile_dd(self, dd, _):
        if self._syncing:
            return
        name = self.prof_model.get_string(dd.get_selected())
        if name and name != self.name:
            self.save_current()
            self.switch_profile(name)

    # ---- syncing model → widgets ----
    def load_into_ui(self):
        self._syncing = True
        p = self.profile
        self.reload_profile_list()
        self.out_row.set_selected(self.out_names.index(p["output"]))
        self.focus_row.set_enable_expansion(p.get("auto_focus", False))
        self.app_row.set_text(p.get("app_id") or "")
        self.grab_row.set_active(p.get("grab", True))
        self.hotkey_btn.set_label(self.hotkey_label() or "未设置")
        self.file_row.set_subtitle(str(prof.PROFILE_DIR / f"{self.name}.json"))
        self._syncing = False
        self.apply()

    def refresh_touch_row(self):
        ok, sub, btn = self.backend.touch_status(self.profile["output"], len(self.outputs))
        self.touch_row.set_subtitle(sub)
        self.touch_icon.set_from_icon_name({True: "emblem-ok-symbolic", False: "dialog-warning-symbolic",
                                            None: "dialog-information-symbolic"}[ok])
        self.touch_btn.set_visible(btn is not None)
        if btn:
            self.touch_btn.set_label(btn)

    def _on_touch_btn(self, *_):
        dlg = Adw.AlertDialog(heading=f"配置 {self.backend.label} 触摸映射",
                              body=self.backend.touch_setup_text(self.profile["output"]))
        dlg.add_response("cancel", "取消")
        dlg.add_response("ok", "确定")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def resp(d, r):
            if r != "ok":
                return
            try:
                self.backend.touch_setup(self.profile["output"])
                self.toast("已写入，桌面会自动重载")
            except OSError as ex:
                self.toast(f"写入失败：{ex}")
            self.refresh_touch_row()

        dlg.connect("response", resp)
        dlg.present(self.win)

    def _on_output(self, row, _):
        self.set_opt("output", self.out_names[row.get_selected()])

    def _fill_window_picker(self, btn):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6,
                      margin_bottom=6, margin_start=6, margin_end=6)
        wins = self.backend.windows()
        seen = set()
        for w in wins:
            app = w.get("app_id")
            if not app or app in seen or app == self.get_application_id():
                continue
            seen.add(app)
            b = Gtk.Button(label=f"{app}  —  {(w.get('title') or '')[:30]}")
            b.add_css_class("flat")

            def pick(_b, app=app):
                regex = "^" + GLib.Regex.escape_string(app, -1) + "$"
                self.app_row.set_text(regex)
                self.set_opt("app_id", regex)
                btn.popdown()

            b.connect("clicked", pick)
            box.append(b)
        if not seen:
            box.append(Gtk.Label(label="没有其他窗口"))
        btn.set_popover(Gtk.Popover(child=box))

    # ---- hotkey capture ----
    def hotkey_label(self):
        p = self.profile
        return combo_label(p["toggle_key"], p.get("toggle_mods")) if p.get("toggle_key") else None

    def _on_hotkey_btn(self, *_):
        self.capturing_hotkey = True
        self.hk_mods.clear()
        self.hk_solo = False
        self.hotkey_btn.set_label("按下按键或组合键… (Esc 取消)")
        self.engine.send(cmd="enable", on=False)  # so the old hotkey can't toggle meanwhile

    def _finish_hotkey(self, name=None, mods=()):
        self.capturing_hotkey = False
        self.hk_mods.clear()
        self.engine.send(cmd="enable", on=self.enable_sw.get_active())
        if name:
            self.profile["toggle_mods"] = [m for m in MOD_ORDER if m in mods]
            self.set_opt("toggle_key", name)
        self.hotkey_btn.set_label(self.hotkey_label() or "未设置")

    def _on_window_key(self, c, keyval, keycode, state):
        if not self.capturing_hotkey:
            return False
        if keyval == Gdk.KEY_Escape:
            self._finish_hotkey()
            return True
        name = key_name(keycode - 8)
        if not name:
            return True
        if name in MOD_NAME_GROUP:
            # Wait: a key may follow (combo), or the modifier is released alone.
            if name not in self.hk_mods:
                self.hk_mods.append(name)
            self.hk_solo = True
            held = "+".join(MOD_LABEL[MOD_NAME_GROUP[m]] for m in self.hk_mods)
            self.hotkey_btn.set_label(f"{held}+…")
            return True
        self._finish_hotkey(name, {MOD_NAME_GROUP[m] for m in self.hk_mods})
        return True

    def _on_window_key_released(self, c, keyval, keycode, state):
        if not self.capturing_hotkey:
            return
        name = key_name(keycode - 8)
        if name not in self.hk_mods:
            return
        if self.hk_solo:
            self._finish_hotkey(name, {MOD_NAME_GROUP[m] for m in self.hk_mods if m != name})
        else:
            self.hk_mods.remove(name)

    # ================================================================ engine
    def _on_enable(self, sw, _):
        if not self._syncing:
            self.engine.send(cmd="enable", on=sw.get_active())

    def on_engine(self, msg):
        ev = msg.get("ev")
        if ev == "key":
            if self.hud:
                self.hud.key_event(msg["code"], msg.get("idx"), msg["down"])
        elif ev == "state":
            self.engine_state = msg
            if not self.synced_after_start:
                # The virtual touchscreen exists now: (re)apply per-device mapping.
                self.synced_after_start = True
                self.backend.touch_sync(self.profile["output"])
                self.refresh_touch_row()
            if not self.editor and not self.capturing_hotkey and self.enable_sw.get_active() != msg["enabled"]:
                self._syncing = True
                self.enable_sw.set_active(msg["enabled"])
                self._syncing = False
            self.refresh_status()
            self.update_hud()
        elif ev == "exit":
            self.status_row.set_title("引擎已退出")
            self.status_row.set_subtitle(f"code {msg.get('code')}，日志：{prof.CACHE_DIR / 'engine.log'}")
            self.status_icon.set_from_icon_name("dialog-error-symbolic")

    def refresh_status(self):
        st = self.engine_state
        if "keyboards" not in st:
            return
        hotkey = self.hotkey_label()
        if self.editor:
            title, icon = "编辑中", "document-edit-symbolic"
        elif st["active"]:
            title, icon = "生效中", "media-playback-start-symbolic"
        elif not st["enabled"]:
            title, icon = "已关闭" + (f"（按 {hotkey} 开启）" if hotkey else ""), "media-playback-pause-symbolic"
        elif not self.profile["points"]:
            title, icon = "没有按键点，先编辑布局", "dialog-information-symbolic"
        else:
            title, icon = "等待目标窗口获得焦点", "content-loading-symbolic"
        n = len(self.profile["points"])
        self.status_row.set_title(title)
        self.status_row.set_subtitle(f"{n} 个按键点 · 焦点 {st.get('focus') or '-'} · "
                                     f"{st.get('keyboards', 0)} 个键盘 · {self.backend.label} · "
                                     f"{st.get('priority', '?')}")
        self.status_icon.set_from_icon_name(icon)

    # ================================================================ HUD / editor
    def _on_hud_setting(self, *_):
        self.settings["hud"] = self.hud_row.get_active()
        self.settings["hud_opacity"] = round(self.opacity.get_value(), 2)
        prof.save_settings(self.settings)
        self.update_hud()

    def rebuild_hud(self):
        if self.editor:
            return
        if self.hud and self.hud.output == self.profile["output"]:
            self.update_hud()  # the HUD reads app.profile, so a redraw is enough
            return
        if self.hud:
            self.hud.close()
        if not self.layer_shell:
            return  # HUD needs wlr-layer-shell (not available on e.g. GNOME)
        self.hud = Overlay(self, edit=False)
        self.hud.present()
        self.update_hud()

    def update_hud(self):
        if self.hud:
            # Shown whenever mapping is on; dimmed while waiting for the game window.
            self.hud.set_hud_visible(self.hud_row.get_active() and self.engine_state.get("enabled", True),
                                     dim=not self.engine_state.get("active", False))

    def on_edit(self, *_):
        if self.editor:
            return
        self.engine.send(cmd="enable", on=False)
        if self.hud:
            self.hud.close()
            self.hud = None
        self.editor = Overlay(self, edit=True)
        self.editor.present()
        self.refresh_status()

    def editor_done(self, save, backup):
        """Called by the editor. `backup` is the current profile as it was when
        the editor opened (or last switched profiles)."""
        self.editor = None
        if not save:
            self.profile = backup
        self.engine.send(cmd="enable", on=self.enable_sw.get_active())
        self.load_into_ui()
        if save:
            self.toast(f"已保存「{self.name}」")
