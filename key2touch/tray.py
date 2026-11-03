"""System tray icon: StatusNotifierItem + com.canonical.dbusmenu over GIO.

No extra dependencies; works with any SNI host (waybar, KDE, the GNOME
AppIndicator extension, ...).  `available()` tells whether a host is running.
"""

import os

from gi.repository import GdkPixbuf, Gio, GLib

WATCHER = "org.kde.StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"

ITEM_XML = """
<node><interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="WindowId" type="i" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="IconThemePath" type="s" access="read"/>
  <property name="IconPixmap" type="a(iiay)" access="read"/>
  <property name="AttentionIconName" type="s" access="read"/>
  <property name="OverlayIconName" type="s" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
  <signal name="NewIcon"/><signal name="NewTitle"/><signal name="NewToolTip"/>
  <signal name="NewStatus"><arg name="status" type="s"/></signal>
</interface></node>
"""

MENU_XML = """
<node><interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout">
    <arg type="i" name="parentId" direction="in"/><arg type="i" name="recursionDepth" direction="in"/>
    <arg type="as" name="propertyNames" direction="in"/>
    <arg type="u" name="revision" direction="out"/><arg type="(ia{sv}av)" name="layout" direction="out"/>
  </method>
  <method name="GetGroupProperties">
    <arg type="ai" name="ids" direction="in"/><arg type="as" name="propertyNames" direction="in"/>
    <arg type="a(ia{sv})" name="properties" direction="out"/>
  </method>
  <method name="GetProperty">
    <arg type="i" name="id" direction="in"/><arg type="s" name="name" direction="in"/>
    <arg type="v" name="value" direction="out"/>
  </method>
  <method name="Event">
    <arg type="i" name="id" direction="in"/><arg type="s" name="eventId" direction="in"/>
    <arg type="v" name="data" direction="in"/><arg type="u" name="timestamp" direction="in"/>
  </method>
  <method name="EventGroup">
    <arg type="a(isvu)" name="events" direction="in"/><arg type="ai" name="idErrors" direction="out"/>
  </method>
  <method name="AboutToShow">
    <arg type="i" name="id" direction="in"/><arg type="b" name="needUpdate" direction="out"/>
  </method>
  <method name="AboutToShowGroup">
    <arg type="ai" name="ids" direction="in"/>
    <arg type="ai" name="updatesNeeded" direction="out"/><arg type="ai" name="idErrors" direction="out"/>
  </method>
  <signal name="LayoutUpdated"><arg type="u" name="revision"/><arg type="i" name="parent"/></signal>
  <signal name="ItemsPropertiesUpdated">
    <arg type="a(ia{sv})" name="updatedProps"/><arg type="a(ias)" name="removedProps"/>
  </signal>
</interface></node>
"""


def available():
    """True if a StatusNotifierWatcher (i.e. some tray) is running."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        res = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                            "NameHasOwner", GLib.Variant("(s)", (WATCHER,)), GLib.VariantType("(b)"),
                            Gio.DBusCallFlags.NONE, 1000, None)
        return res.unpack()[0]
    except GLib.Error:
        return False


def _pixmaps(svg_path):
    """SNI wants ARGB32 in network byte order; some hosts ignore IconName."""
    out = []
    if not svg_path or not os.path.exists(svg_path):
        return out
    for size in (22, 32, 48, 64):
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_size(svg_path, size, size)
        except GLib.Error:
            continue
        if not pb.get_has_alpha():
            pb = pb.add_alpha(False, 0, 0, 0)
        w, h, stride = pb.get_width(), pb.get_height(), pb.get_rowstride()
        px = pb.get_pixels()
        argb = bytearray()
        for y in range(h):
            row = px[y * stride:y * stride + w * 4]
            for x in range(0, w * 4, 4):
                r, g, b, a = row[x:x + 4]
                argb += bytes((a, r, g, b))
        out.append((w, h, bytes(argb)))
    return out


class Tray:
    """items: list of dicts {label, action, checked: callable | None} or None for a separator."""

    def __init__(self, app_id, title, icon_name, icon_dir, items, on_activate):
        self.app_id, self.title, self.icon_name = app_id, title, icon_name
        self.icon_dir = icon_dir or ""
        self.items = items
        self.on_activate = on_activate
        self.revision = 1
        self.pixmaps = _pixmaps(os.path.join(self.icon_dir, f"{icon_name}.svg"))
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.reg_ids = [
            self.bus.register_object(ITEM_PATH, Gio.DBusNodeInfo.new_for_xml(ITEM_XML).interfaces[0],
                                     self._item_call, self._item_prop, None),
            self.bus.register_object(MENU_PATH, Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0],
                                     self._menu_call, self._menu_prop, None),
        ]
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.own_id = Gio.bus_own_name_on_connection(self.bus, self.name, Gio.BusNameOwnerFlags.NONE,
                                                     self._register, None)

    def _register(self, *_):
        self.bus.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                      GLib.Variant("(s)", (self.name,)), None, Gio.DBusCallFlags.NONE, -1, None, None)

    def close(self):
        for rid in self.reg_ids:
            self.bus.unregister_object(rid)
        Gio.bus_unown_name(self.own_id)

    # ---- StatusNotifierItem ----
    def _item_prop(self, conn, sender, path, iface, prop):
        values = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", self.app_id),
            "Title": GLib.Variant("s", self.title),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", self.icon_name),
            "IconThemePath": GLib.Variant("s", self.icon_dir),
            "IconPixmap": GLib.Variant("a(iiay)", self.pixmaps),
            "AttentionIconName": GLib.Variant("s", ""),
            "OverlayIconName": GLib.Variant("s", ""),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", ("", [], self.title, "")),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return values.get(prop)

    def _item_call(self, conn, sender, path, iface, method, params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            self.on_activate()
        invocation.return_value(None)

    # ---- dbusmenu ----
    def _menu_prop(self, conn, sender, path, iface, prop):
        return {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                "Status": GLib.Variant("s", "normal"), "IconThemePath": GLib.Variant("as", [])}.get(prop)

    def _props(self, i):
        item = self.items[i - 1]
        if item is None:
            return {"type": GLib.Variant("s", "separator")}
        props = {"label": GLib.Variant("s", item["label"]), "enabled": GLib.Variant("b", True),
                 "visible": GLib.Variant("b", True)}
        if item.get("checked"):
            props["toggle-type"] = GLib.Variant("s", "checkmark")
            props["toggle-state"] = GLib.Variant("i", 1 if item["checked"]() else 0)
        return props

    def _layout(self):
        children = [GLib.Variant("(ia{sv}av)", (i, self._props(i), []))
                    for i in range(1, len(self.items) + 1)]
        return (0, {"children-display": GLib.Variant("s", "submenu")}, children)

    def _menu_call(self, conn, sender, path, iface, method, params, invocation):
        if method == "GetLayout":
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, self._layout())))
        elif method == "GetGroupProperties":
            ids = params.unpack()[0] or range(1, len(self.items) + 1)
            res = [(i, self._props(i)) for i in ids if 1 <= i <= len(self.items)]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (res,)))
        elif method == "GetProperty":
            i, name = params.unpack()
            invocation.return_value(GLib.Variant("(v)", (self._props(i).get(name, GLib.Variant("s", "")),)))
        elif method == "Event":
            i, event = params.unpack()[:2]
            if event == "clicked" and 1 <= i <= len(self.items) and self.items[i - 1]:
                GLib.idle_add(self.items[i - 1]["action"])
            invocation.return_value(None)
        elif method == "EventGroup":
            for i, event, *_ in params.unpack()[0]:
                if event == "clicked" and 1 <= i <= len(self.items) and self.items[i - 1]:
                    GLib.idle_add(self.items[i - 1]["action"])
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_value(None)

    def refresh(self):
        """Call after a checked() state changes."""
        self.revision += 1
        self.bus.emit_signal(None, MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                             GLib.Variant("(ui)", (self.revision, 0)))
