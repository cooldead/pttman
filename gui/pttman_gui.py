"""Desktop controls embedded in `pttman gui`. Requires PyGObject and GTK 4."""
import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gio, GLib, Gtk

CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "pttman.conf"
SOCKET = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "pttman.sock"
PORTAL = "org.freedesktop.portal.Desktop"
OBJECT = "/org/freedesktop/portal/desktop"
SHORTCUTS = "org.freedesktop.portal.GlobalShortcuts"
SHORTCUT_ENABLED = CONFIG.parent / "pttman-gui-shortcut-enabled"
GUI_PREFS = CONFIG.parent / "pttman-gui.json"
AUTOSTART = CONFIG.parent / "autostart/io.github.mwolson.pttman.desktop"
# Shortcut keys can chatter release/press ~20-50ms apart mid-hold; a re-press inside this window continues the hold.
RELEASE_DEBOUNCE_MS = 100


def playback_command(path, volume=100):
    volume = max(0, min(100, int(volume)))
    path = str(path)
    if Path(path).suffix.lower() == ".mp3":
        return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", "-volume", str(volume), "-i", path]
    return ["paplay", f"--volume={(volume * 65536 + 50) // 100}", "--", path]


def read_config():
    values = {}
    if CONFIG.exists():
        for line in CONFIG.read_text().splitlines():
            if line.strip().startswith("--") and "=" in line:
                key, value = line.strip().split("=", 1)
                values[key] = value
    return values


def save_config(values):
    for value in values.values():
        if "\n" in value or "\r" in value:
            raise ValueError("Settings cannot contain line breaks")
    target = CONFIG.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = target.read_text().splitlines() if target.exists() else []
    lines = [line for line in lines if line.strip().split("=", 1)[0] not in values]
    lines.extend(f"{key}={value}" for key, value in sorted(values.items()) if value)
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=".pttman-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write("\n".join(lines) + "\n")
        if target.exists():
            os.chmod(name, target.stat().st_mode & 0o777)
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def send(action):
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
        client.settimeout(0.1)
        client.sendto(action.encode(), str(SOCKET))


def running():
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
            client.connect(str(SOCKET))
        return True
    except OSError:
        return False


class GlobalShortcut:
    def __init__(self, edge, report, ready):
        self.edge, self.report = edge, report
        self.ready = ready
        self.pending = False
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.session = None
        self.bus.signal_subscribe(PORTAL, SHORTCUTS, None, OBJECT, None,
                                  Gio.DBusSignalFlags.NONE, self.signal)

    def request(self, method, signature, args, callback, options=None):
        token = "pttman_" + uuid.uuid4().hex
        options = dict(options or {}, handle_token=GLib.Variant("s", token))
        sender = self.bus.get_unique_name()[1:].replace(".", "_")
        path = f"{OBJECT}/request/{sender}/{token}"
        subscription = None

        def response(bus, sender, path, interface, signal, params, *unused):
            bus.signal_unsubscribe(subscription)
            code, results = params.unpack()
            if code == 0:
                callback(results)
            else:
                self.pending = False
                self.report("Shortcut setup cancelled. You can try again.")

        subscription = self.bus.signal_subscribe(
            PORTAL, "org.freedesktop.portal.Request", "Response", path, None,
            Gio.DBusSignalFlags.NONE, response)

        def finished(bus, result, *unused):
            try:
                bus.call_finish(result)
            except GLib.Error as error:
                bus.signal_unsubscribe(subscription)
                self.pending = False
                self.report(f"Global shortcut unavailable: {error.message}")

        self.bus.call(PORTAL, OBJECT, SHORTCUTS, method,
                      GLib.Variant(signature, (*args, options)), None,
                      Gio.DBusCallFlags.NONE, -1, None, finished)

    def setup(self):
        if self.pending:
            return
        if self.session:
            def configured(bus, result, *unused):
                try:
                    bus.call_finish(result)
                except GLib.Error:
                    self.report("Change this shortcut in your desktop's shortcut settings.")
            self.bus.call(PORTAL, OBJECT, SHORTCUTS, "ConfigureShortcuts",
                          GLib.Variant("(osa{sv})", (self.session, "", {})), None,
                          Gio.DBusCallFlags.NONE, -1, None, configured)
            return
        self.close()
        self.pending = True
        self.report("Choose a shortcut in the desktop dialog…")
        self.request("CreateSession", "(a{sv})", (), self.created, {
            "session_handle_token": GLib.Variant("s", "pttman_" + uuid.uuid4().hex)})

    def created(self, results):
        self.session = results["session_handle"]
        self.bus.signal_subscribe(PORTAL, "org.freedesktop.portal.Session", "Closed",
                                  self.session, None, Gio.DBusSignalFlags.NONE, self.closed)
        self.request("BindShortcuts", "(oa(sa{sv})sa{sv})", (
            self.session, [("talk", {"description": GLib.Variant("s", "Hold to talk"),
                                      "preferred_trigger": GLib.Variant("s", "F9")})], ""), self.bound)

    def bound(self, results):
        self.pending = False
        shortcuts = results.get("shortcuts", [])
        for name, props in shortcuts:
            if name == "talk":
                self.report("Global shortcut: " + props.get("trigger_description", "configured"))
                self.ready()
                return
        self.report("No shortcut assigned. Try configuring it again.")

    def signal(self, bus, sender, path, interface, signal, params, *unused):
        data = params.unpack()
        if data[0] != self.session:
            return
        if signal in ("Activated", "Deactivated") and data[1] == "talk":
            self.edge("global", signal == "Activated")
        elif signal == "ShortcutsChanged":
            self.bound({"shortcuts": data[1]})

    def closed(self, *args):
        # Ignore a delayed Closed signal from a session we replaced.
        if args[2] == self.session:
            self.session = None
            self.edge("global", False)
            self.report("Global shortcut session ended. Configure it again.")

    def close(self):
        if self.session:
            session, self.session = self.session, None
            self.edge("global", False)
            self.bus.call(PORTAL, session, "org.freedesktop.portal.Session", "Close",
                          None, None, Gio.DBusCallFlags.NONE, -1, None, None)


def lamp_svg(state):
    live = state == "live"
    color = "#ff263b" if live else "#49131c" if state == "muted" else "#776247"
    core = "#fff0ca" if live else "#85212d" if state == "muted" else "#c89850"
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">
      <defs><radialGradient id="glass"><stop stop-color="{core}"/><stop offset=".55" stop-color="{color}"/><stop offset="1" stop-color="#370e18"/></radialGradient></defs>
      <ellipse cx="32" cy="33" rx="28" ry="29" fill="{color}" opacity="{'.28' if live else '.06'}"/>
      <rect x="17" y="3" width="30" height="6" rx="3" fill="#4b5059"/>
      <rect x="17" y="55" width="30" height="6" rx="3" fill="#4b5059"/>
      <rect x="9" y="7" width="46" height="50" rx="22" fill="#191c24" stroke="#89909b" stroke-width="2"/>
      <rect x="14" y="11" width="36" height="42" rx="18" fill="url(#glass)"/>
      <ellipse cx="25" cy="20" rx="6" ry="4" fill="#ffffff" opacity="{'.55' if live else '.12'}"/>
      <path d="M22 10 Q17 32 22 54 M42 10 Q47 32 42 54 M11 25 H53 M11 40 H53" fill="none" stroke="#292c35" stroke-width="3"/>
      <path d="M23 11 Q19 31 23 52" fill="none" stroke="#aeb1b8" stroke-width="1" opacity=".5"/>
    </svg>'''


def lamp_pixmaps(state):
    from gi.repository import GdkPixbuf
    images = []
    for size in (22, 32, 48, 64):
        loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
        loader.set_size(size, size)
        loader.write(lamp_svg(state).encode())
        loader.close()
        pixbuf = loader.get_pixbuf()
        pixels = pixbuf.get_pixels()
        stride, channels = pixbuf.get_rowstride(), pixbuf.get_n_channels()
        argb = bytearray()
        for y in range(size):
            for x in range(size):
                offset = y * stride + x * channels
                r, g, b = pixels[offset:offset + 3]
                a = pixels[offset + 3] if channels == 4 else 255
                argb.extend((a, r, g, b))
        images.append((size, size, bytes(argb)))
    return images


class Tray:
    ITEM = "org.kde.StatusNotifierItem"
    MENU = "com.canonical.dbusmenu"
    WATCHER = "org.kde.StatusNotifierWatcher"
    XML = '''<node><interface name="org.kde.StatusNotifierItem">
      <property name="Category" type="s" access="read"/>
      <property name="Id" type="s" access="read"/>
      <property name="Title" type="s" access="read"/>
      <property name="Status" type="s" access="read"/>
      <property name="WindowId" type="i" access="read"/>
      <property name="IconName" type="s" access="read"/>
      <property name="IconPixmap" type="a(iiay)" access="read"/>
      <property name="OverlayIconName" type="s" access="read"/>
      <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
      <property name="AttentionIconName" type="s" access="read"/>
      <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
      <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
      <property name="Menu" type="o" access="read"/>
      <property name="ItemIsMenu" type="b" access="read"/>
      <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
      <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
      <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
      <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
      <method name="ProvideXdgActivationToken"><arg type="s" direction="in"/></method>
      <signal name="NewIcon"/><signal name="NewToolTip"/>
    </interface><interface name="com.canonical.dbusmenu">
      <property name="Version" type="u" access="read"/>
      <property name="TextDirection" type="s" access="read"/>
      <property name="Status" type="s" access="read"/>
      <property name="IconThemePath" type="as" access="read"/>
      <method name="GetLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/><arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/></method>
      <method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/><arg type="a(ia{sv})" direction="out"/></method>
      <method name="GetProperty"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
      <method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/><arg type="u" direction="in"/></method>
      <method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
      <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
      <method name="AboutToShowGroup"><arg type="ai" direction="in"/><arg type="ai" direction="out"/><arg type="ai" direction="out"/></method>
      <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
    </interface></node>'''

    def __init__(self, app):
        self.app = app
        self.active = False
        self.state, self.description = "unknown", "Checking microphone…"
        self.images = {state: lamp_pixmaps(state) for state in ("live", "muted", "unknown")}
        self.items = {
            1: ("Open settings", app.activate),
            2: ("Mute microphone", lambda: app.action("mute")),
            3: ("Unmute microphone", lambda: app.action("unmute")),
            4: ("Toggle mute", lambda: app.action("toggle")),
            5: ("Reapply microphone state", lambda: app.action("resync")),
            6: ("Quit pttman GUI", app.quit),
        }
        # A private connection lets close() drop the bus name so the watcher removes the icon.
        self.bus = Gio.DBusConnection.new_for_address_sync(
            Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None),
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None)
        info = Gio.DBusNodeInfo.new_for_xml(self.XML)
        self.objects = [self.bus.register_object(path, info.lookup_interface(interface), self.call, self.property, None)
                        for path, interface in (("/StatusNotifierItem", self.ITEM), ("/Menu", self.MENU))]
        self.watch = Gio.bus_watch_name_on_connection(self.bus, self.WATCHER, Gio.BusNameWatcherFlags.NONE,
                                                       self.appeared, self.vanished)

    def appeared(self, *args):
        def registered(bus, result, *args):
            try:
                bus.call_finish(result)
                self.active = True
                if self.app.start_hidden:
                    self.app.window.set_visible(False)
                    self.app.start_hidden = False
            except GLib.Error as error:
                self.app.report(f"Tray registration failed: {error.message}")
        self.bus.call(self.WATCHER, "/StatusNotifierWatcher", self.WATCHER, "RegisterStatusNotifierItem",
                      GLib.Variant("(s)", ("/StatusNotifierItem",)), None, Gio.DBusCallFlags.NONE,
                      5000, None, registered)

    def vanished(self, *args):
        self.active = False
        if hasattr(self.app, "window") and not self.app.closing:
            self.app.window.present()

    def property(self, bus, sender, path, interface, name, *args):
        if interface == self.MENU:
            return {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                    "Status": GLib.Variant("s", "normal"), "IconThemePath": GLib.Variant("as", [])}.get(name)
        values = {"Category": ("s", "Hardware"), "Id": ("s", "pttman"), "Title": ("s", "pttman"),
                  "Status": ("s", "Active"), "WindowId": ("i", 0), "Menu": ("o", "/Menu"),
                  "ItemIsMenu": ("b", False), "IconName": ("s", ""),
                  "IconPixmap": ("a(iiay)", self.images[self.state]),
                  "OverlayIconName": ("s", ""), "OverlayIconPixmap": ("a(iiay)", []),
                  "AttentionIconName": ("s", ""), "AttentionIconPixmap": ("a(iiay)", []),
                  "ToolTip": ("(sa(iiay)ss)", ("", self.images[self.state], "pttman", self.description))}
        return GLib.Variant(*values[name]) if name in values else None

    def properties(self, item, names=()):
        values = {"children-display": GLib.Variant("s", "submenu")} if item == 0 else {
            "label": GLib.Variant("s", self.items[item][0]), "enabled": GLib.Variant("b", True),
            "visible": GLib.Variant("b", True)}
        return {key: value for key, value in values.items() if not names or key in names}

    def layout(self, item, depth, names):
        children = [GLib.Variant("(ia{sv}av)", (key, self.properties(key, names), []))
                    for key in self.items] if item == 0 and depth != 0 else []
        return item, self.properties(item, names), children

    def event(self, item, event, *args):
        if item in self.items and event == "clicked":
            GLib.idle_add(lambda: (self.items[item][1](), False)[1])

    def call(self, bus, sender, path, interface, method, params, invocation, *args):
        try:
            data = params.unpack()
            result = None
            if interface == self.ITEM:
                if method in ("Activate", "ContextMenu"):
                    self.app.activate()
                elif method == "SecondaryActivate":
                    self.app.action("toggle")
            elif method == "GetLayout":
                result = GLib.Variant("(u(ia{sv}av))", (1, self.layout(*data)))
            elif method == "GetGroupProperties":
                ids, names = data
                result = GLib.Variant("(a(ia{sv}))", ([(key, self.properties(key, names)) for key in (ids or [0, *self.items])],))
            elif method == "GetProperty":
                result = GLib.Variant("(v)", (self.properties(data[0])[data[1]],))
            elif method == "Event":
                self.event(*data)
            elif method == "EventGroup":
                for event in data[0]:
                    self.event(*event)
                result = GLib.Variant("(ai)", ([],))
            elif method == "AboutToShow":
                result = GLib.Variant("(b)", (False,))
            elif method == "AboutToShowGroup":
                result = GLib.Variant("(aiai)", ([], []))
            invocation.return_value(result)
        except (KeyError, ValueError, TypeError) as error:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.InvalidArgs", str(error))

    def update(self, state, description):
        if (state, description) == (self.state, self.description):
            return
        self.state, self.description = state, description
        for signal in ("NewIcon", "NewToolTip"):
            self.bus.emit_signal(None, "/StatusNotifierItem", self.ITEM, signal, None)

    def close(self):
        Gio.bus_unwatch_name(self.watch)
        for object_id in self.objects:
            self.bus.unregister_object(object_id)
        self.bus.close_sync(None)


def microphone_state(text):
    states = [line for line in text.splitlines() if line.startswith("source ") and line.endswith(" *")]
    if any(": unmuted" in line for line in states):
        return "live", "Microphone live"
    if states and all(": muted" in line for line in states):
        return "muted", "Microphone muted"
    return "unknown", "Microphone state unavailable"


def validate_timeout(value):
    import re
    value = value.strip()
    if value in ("off", "0", "false", "none"):
        return "off"
    match = re.fullmatch(r"([0-9]+)(ms|s|m|h)?", value)
    if not match:
        raise ValueError("Maximum talk time must be off or a duration such as 500ms, 120s, 2m, or 1h.")
    multiplier = {None: 1000, "ms": 1, "s": 1000, "m": 60000, "h": 3600000}[match[2]]
    if int(match[1]) * multiplier > 2**64 - 1:
        raise ValueError("Maximum talk time is too large.")
    return value


class App(Gtk.Application):
    def __init__(self, binary):
        super().__init__(application_id="io.github.mwolson.pttman")
        self.binary = binary
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=3)
        self.held = set()
        self.pending_release = None
        self.preview = None
        self.daemon = None
        self.polling = False
        self.closing = False
        self.portal = None
        self.tray = None
        self.start_hidden = os.environ.get("PTTMAN_START_HIDDEN") == "1"
        self.connect("activate", self.activate)
        self.connect("shutdown", self.shutdown)

    def label(self, text, css=None):
        widget = Gtk.Label(label=text, xalign=0, wrap=True)
        if css:
            widget.add_css_class(css)
        return widget

    def button(self, text, callback):
        button = Gtk.Button(label=text)
        button.connect("clicked", lambda *_: callback())
        return button

    def section(self, title):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.add_css_class("card")
        box.append(self.label(title, "heading"))
        self.content.append(box)
        return box

    def activate(self, *_):
        if hasattr(self, "window"):
            self.window.present()
            return
        self.window = Gtk.ApplicationWindow(application=self, title="pttman")
        self.window.set_default_size(560, 740)
        self.window.connect("close-request", self.close_window)
        self.window.connect("notify::is-active", self.focus_changed)
        css = Gtk.CssProvider()
        css.load_from_data(b".card { padding: 18px; border-radius: 12px; background: alpha(@theme_fg_color, 0.045); } .title { font-size: 28px; font-weight: bold; } .status { font-size: 20px; font-weight: bold; } .talk { padding: 22px; font-size: 18px; font-weight: bold; } .heading { font-weight: bold; }")
        Gtk.StyleContext.add_provider_for_display(self.window.get_display(), css, 600)
        scroll = Gtk.ScrolledWindow()
        self.window.set_child(scroll)
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        for side in ("top", "bottom", "start", "end"):
            getattr(self.content, f"set_margin_{side}")(24)
        scroll.set_child(self.content)
        self.content.append(self.label("pttman", "title"))
        self.content.append(self.label("Your microphone, on your terms."))
        control = self.section("MICROPHONE")
        self.status = self.label("Checking microphone…", "status")
        control.append(self.status)
        self.sources = Gtk.DropDown.new_from_strings(["All microphones"])
        self.source_names = [""]
        control.append(self.sources)
        control.append(self.button("Refresh microphones", self.refresh_sources))
        self.talk = Gtk.Button(label="Hold to talk")
        self.talk.add_css_class("talk")
        self.talk.add_css_class("suggested-action")
        gesture = Gtk.GestureClick(button=1)
        gesture.connect("pressed", lambda *_: self.edge("button", True))
        gesture.connect("released", lambda *_: self.edge("button", False))
        gesture.connect("cancel", lambda *_: self.edge("button", False))
        self.talk.add_controller(gesture)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.key_pressed)
        keys.connect("key-released", self.key_released)
        self.talk.add_controller(keys)
        control.append(self.talk)
        row = Gtk.Box(spacing=10, homogeneous=True)
        row.append(self.button("Mute", lambda: self.action("mute")))
        row.append(self.button("Unmute", lambda: self.action("unmute")))
        row.append(self.button("Start control", self.start_daemon))
        control.append(row)
        row = Gtk.Box(spacing=10, homogeneous=True)
        row.append(self.button("Toggle mute", lambda: self.action("toggle")))
        row.append(self.button("Reapply state", lambda: self.action("resync")))
        control.append(row)
        hotkey = self.section("GLOBAL PUSH-TO-TALK")
        self.shortcut_status = self.label("Works while another app is focused, including when pttman is in the tray.")
        hotkey.append(self.shortcut_status)
        hotkey.append(self.button("Configure global shortcut…", self.configure_shortcut))
        hotkey.append(self.button("Disable global shortcut", self.disable_shortcut))
        hotkey.append(self.label("The desktop chooses available keys. For a mouse button, map it to your chosen key in your mouse software."))
        audio = self.section("SOUND VOLUME")
        self.sound_volume = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.sound_volume.set_digits(0)
        self.sound_volume.set_value(100)
        self.sound_volume.set_hexpand(True)
        audio.append(self.sound_volume)
        audio.append(self.label("0–100% · Applies to press and release sounds. Preview to hear the level, then save."))
        self.sound_controls = {}
        for edge, description in (("press", "pressed"), ("release", "released")):
            sound = self.section(f"{edge.upper()} SOUND")
            enabled = Gtk.CheckButton(label=f"Play a sound when push-to-talk is {description}")
            sound.append(enabled)
            path = Gtk.Entry(placeholder_text="Choose an MP3, WAV, OGG, or FLAC file")
            sound.append(path)
            self.sound_controls[edge] = (enabled, path)
            row = Gtk.Box(spacing=10)
            row.append(self.button("Choose sound…", lambda edge=edge: self.choose_sound(edge)))
            row.append(self.button("Preview", lambda edge=edge: self.preview_sound(edge)))
            sound.append(row)
        safety = self.section("MICROPHONE SETTINGS")
        safety.append(self.label("Maximum talk time · off, milliseconds, seconds, minutes, or hours"))
        self.timeout = Gtk.Entry(placeholder_text="off, 500ms, 120s, 2m, or 1h")
        self.timeout.set_text("off")
        safety.append(self.timeout)
        safety.append(self.label("A timeout mutes the microphone if a key release is missed."))
        self.start_muted = Gtk.CheckButton(label="Start with microphones muted")
        safety.append(self.start_muted)
        desktop = self.section("DESKTOP")
        self.show_tray = Gtk.CheckButton(label="Show the tray icon")
        self.show_tray.set_active(True)
        self.show_tray.connect("toggled", self.toggle_tray)
        desktop.append(self.show_tray)
        desktop.append(self.label("With the icon hidden, run `pttman gui` to reopen this window."))
        self.keep_in_tray = Gtk.CheckButton(label="Keep running in the background when the window closes")
        self.keep_in_tray.set_active(True)
        desktop.append(self.keep_in_tray)
        self.autostart = Gtk.CheckButton(label="Start the tray icon and global shortcut when I log in")
        self.autostart.set_active(AUTOSTART.exists())
        desktop.append(self.autostart)
        desktop.append(self.label("Red light on: microphone live. Dark red: muted. Amber: unavailable."))
        desktop.append(self.button("Quit pttman GUI", self.quit))
        service = self.section("BACKGROUND SERVICE")
        self.service_status = self.label("Checking service…")
        service.append(self.service_status)
        for actions in (("start", "stop", "restart"), ("enable", "disable"), ("install", "uninstall")):
            row = Gtk.Box(spacing=8, homogeneous=True)
            for action in actions:
                title = {"enable": "Enable at login", "disable": "Disable at login"}.get(action, action.title())
                row.append(self.button(title, lambda action=action: self.service_action(action)))
            service.append(row)
        service.append(self.label("Service login settings control the microphone daemon. Desktop login settings control the tray and shortcut."))
        save = self.button("Save settings", self.save)
        save.add_css_class("suggested-action")
        self.content.append(save)
        self.message = self.label("")
        self.content.append(self.message)
        try:
            config = read_config()
            self.sound_volume.set_value(int(config.get("--sound-volume", "100")))
            for edge, (enabled, entry) in self.sound_controls.items():
                path = config.get(f"--{edge}-sound", "off")
                enabled.set_active(path != "off")
                entry.set_text("" if path == "off" else path)
            self.start_muted.set_active(config.get("--start-muted", "true") == "true")
            self.timeout.set_text(config.get("--ptt-hold-timeout", "off"))
            if GUI_PREFS.exists():
                prefs = json.loads(GUI_PREFS.read_text())
                self.keep_in_tray.set_active(prefs.get("keep_in_tray", True))
                self.show_tray.set_active(prefs.get("show_tray", True))
        except (OSError, ValueError) as error:
            self.report(str(error))
        self.toggle_tray()
        self.refresh_sources()
        self.refresh_service()
        self.poll()
        GLib.timeout_add(300, self.poll)
        # Without a tray icon, a login start stays hidden; `pttman gui` reopens the window.
        if not (self.start_hidden and not self.tray):
            self.window.present()
        if SHORTCUT_ENABLED.exists():
            self.configure_shortcut()

    def toggle_tray(self, *_):
        show = self.show_tray.get_active()
        if show and not self.tray:
            try:
                self.tray = Tray(self)
            except (GLib.Error, ValueError) as error:
                self.report(f"Tray unavailable: {error}")
        elif not show and self.tray:
            self.tray.close()
            self.tray = None

    def report(self, text):
        self.message.set_text(text)

    def background(self, work, callback):
        future = self.pool.submit(work)
        def done(future):
            def deliver():
                if not self.closing:
                    try:
                        callback(future.result())
                    except Exception as error:
                        self.report(str(error))
                return False
            GLib.idle_add(deliver)
        future.add_done_callback(done)

    def command(self, args):
        result = subprocess.run(args, capture_output=True, text=True, timeout=5,
                                env=dict(os.environ, LC_ALL="C"))
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "Command failed")
        return result.stdout

    def refresh_sources(self):
        def update(text):
            devices = [s for s in json.loads(text) if not s["name"].endswith(".monitor")]
            selected = read_config().get("--source", "")
            self.source_names = ["", "@DEFAULT_SOURCE@"] + [s["name"] for s in devices]
            labels = ["All microphones", "Follow system default microphone"] + [s.get("description", s["name"]) for s in devices]
            if selected and selected not in self.source_names:
                self.source_names.append(selected)
                labels.append(selected + " (disconnected)")
            self.sources.set_model(Gtk.StringList.new(labels))
            self.sources.set_selected(self.source_names.index(selected))
        self.background(lambda: self.command(["pactl", "--format=json", "list", "sources"]), update)

    def poll(self):
        if self.closing:
            return False
        if self.polling:
            return True
        self.polling = True
        def work():
            try:
                return running(), self.command([self.binary, "status"])
            except Exception as error:
                return running(), str(error)
        def update(result):
            self.polling = False
            alive, text = result
            state, status = microphone_state(text)
            if not alive:
                status += " · control stopped"
                self.held.clear()
            self.status.set_text(status)
            if self.tray:
                self.tray.update(state, status)
            if self.daemon and self.daemon.poll() is not None:
                self.daemon = None
        self.background(work, update)
        return True

    def action(self, action):
        try:
            send(action)
            self.report("")
        except OSError:
            self.report("Start microphone control first.")
            return False
        self.poll()
        return True

    def edge(self, origin, pressed):
        before = bool(self.held)
        if pressed:
            self.held.add(origin)
        else:
            self.held.discard(origin)
        after = bool(self.held)
        if before == after:
            return
        if not after:
            self.pending_release = GLib.timeout_add(RELEASE_DEBOUNCE_MS, self.send_release)
        elif self.pending_release:
            GLib.source_remove(self.pending_release)
            self.pending_release = None
        elif not self.action("press"):
            self.held.clear()
        self.talk.set_label("Talking… release to mute" if self.held else "Hold to talk")

    def send_release(self):
        self.pending_release = None
        self.action("release")
        return False

    def key_pressed(self, controller, key, code, state):
        if key in (32, 65293):
            self.edge("key", True)
            return True
        return False

    def key_released(self, controller, key, code, state):
        if key in (32, 65293):
            self.edge("key", False)

    def focus_changed(self, *_):
        if not self.window.is_active():
            self.edge("button", False)
            self.edge("key", False)

    def start_daemon(self):
        if running():
            self.report("Microphone control is already running.")
            return
        if not self.save():
            return
        try:
            self.daemon = subprocess.Popen([self.binary], stdin=subprocess.DEVNULL,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.report("Starting microphone control with your saved settings.")
        except OSError as error:
            self.report(str(error))

    def refresh_service(self):
        if not shutil.which("systemctl"):
            self.service_status.set_text("Service controls require systemd. Use pttman install-service for OpenRC.")
            return
        def work():
            return self.command(["systemctl", "--user", "show", "pttman.service", "-p", "ActiveState", "-p", "UnitFileState"])
        self.background(work, lambda text: self.service_status.set_text(text.strip().replace("\n", " · ")))

    def service_action(self, action):
        if self.held:
            self.report("Release push-to-talk before changing the service.")
            return
        if not shutil.which("systemctl"):
            self.report("These service controls require systemd.")
            return
        self.report(f"Service: {action}…")
        def work():
            if self.daemon and self.daemon.poll() is None and action in ("stop", "restart", "uninstall"):
                self.command([self.binary, "mute"])
                self.daemon.terminate()
                self.daemon.wait(timeout=3)
                self.daemon = None
                self.command([self.binary, "mute"])
                if action == "stop":
                    return "Stopped GUI daemon"
                if action == "restart":
                    self.daemon = subprocess.Popen([self.binary], stdin=subprocess.DEVNULL,
                                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return "Restarted GUI daemon"
            if action == "install":
                target = Path.home() / ".local/bin/pttman"
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.resolve() != Path(self.binary).resolve():
                    staged = target.with_suffix(".new")
                    shutil.copy2(self.binary, staged)
                    os.replace(staged, target)
                return self.command([str(target), "install-service"])
            if action in ("stop", "uninstall"):
                self.command([self.binary, "mute"])
            if action == "uninstall":
                result = self.command([self.binary, "uninstall-service"])
            else:
                result = self.command(["systemctl", "--user", action, "pttman.service"])
            if action in ("stop", "uninstall"):
                self.command([self.binary, "mute"])
            return result
        def done(text):
            self.report(f"Service {action} completed.")
            self.refresh_service()
            self.poll()
        self.background(work, done)

    def disable_shortcut(self):
        if self.portal:
            self.portal.close()
        SHORTCUT_ENABLED.unlink(missing_ok=True)
        self.shortcut_status.set_text("Global shortcut disabled.")

    def configure_shortcut(self):
        try:
            if self.portal is None:
                self.portal = GlobalShortcut(self.edge, self.shortcut_status.set_text, self.remember_shortcut)
            self.portal.setup()
        except GLib.Error as error:
            self.report(f"Could not connect to desktop shortcuts: {error.message}")

    def remember_shortcut(self):
        try:
            SHORTCUT_ENABLED.parent.mkdir(parents=True, exist_ok=True)
            SHORTCUT_ENABLED.touch()
        except OSError as error:
            self.report(f"Shortcut works, but could not remember it for next launch: {error}")

    def choose_sound(self, edge):
        dialog = Gtk.FileChooserNative(title=f"Choose {edge} sound", transient_for=self.window,
                                        action=Gtk.FileChooserAction.OPEN)
        audio = Gtk.FileFilter()
        audio.set_name("Audio (MP3, WAV, OGG, FLAC)")
        for pattern in ("*.mp3", "*.wav", "*.ogg", "*.flac", "*.MP3", "*.WAV", "*.OGG", "*.FLAC"):
            audio.add_pattern(pattern)
        dialog.add_filter(audio)
        def selected(dialog, response):
            if response == Gtk.ResponseType.ACCEPT:
                enabled, entry = self.sound_controls[edge]
                entry.set_text(dialog.get_file().get_path())
                enabled.set_active(True)
            dialog.destroy()
        dialog.connect("response", selected)
        dialog.show()

    def selected_sound(self, edge):
        path = Path(self.sound_controls[edge][1].get_text()).expanduser()
        if not path.is_file():
            raise ValueError("Choose an existing sound file first.")
        player = playback_command(path)[0]
        if not shutil.which(player):
            raise ValueError(f"Install {player} ({'FFmpeg' if player == 'ffplay' else 'PulseAudio utilities'}) to play this sound.")
        return str(path.resolve())

    def preview_sound(self, edge):
        try:
            path = self.selected_sound(edge)
            if self.preview and self.preview.poll() is None:
                self.preview.terminate()
                self.preview.wait(timeout=1)
            self.preview = subprocess.Popen(playback_command(path, self.sound_volume.get_value()), stdin=subprocess.DEVNULL, stderr=subprocess.PIPE)
            player = self.preview
            def work():
                try:
                    _, error = player.communicate(timeout=30)
                except subprocess.TimeoutExpired:
                    player.kill()
                    player.communicate()
                    return "Preview stopped after 30 seconds. Use a short sound."
                return "Preview finished." if player.returncode == 0 else error.decode().strip()
            self.background(work, self.report)
        except (OSError, ValueError) as error:
            self.report(str(error))

    def save(self):
        if self.held:
            self.report("Release push-to-talk before changing settings.")
            return False
        try:
            sounds = {f"--{edge}-sound": self.selected_sound(edge) if enabled.get_active() else "off"
                      for edge, (enabled, _) in self.sound_controls.items()}
            source = self.source_names[self.sources.get_selected()]
            save_config({"--all-sources": "true" if not source else "",
                         "--source": source, **sounds,
                         "--sound-volume": str(round(self.sound_volume.get_value())),
                         "--start-muted": str(self.start_muted.get_active()).lower(),
                         "--ptt-hold-timeout": validate_timeout(self.timeout.get_text())})
            GUI_PREFS.parent.mkdir(parents=True, exist_ok=True)
            GUI_PREFS.write_text(json.dumps({"keep_in_tray": self.keep_in_tray.get_active(),
                                              "show_tray": self.show_tray.get_active()}))
            if self.autostart.get_active():
                AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
                binary = self.binary.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "\\$").replace("`", "\\`")
                AUTOSTART.write_text('[Desktop Entry]\nType=Application\nName=pttman\n'
                                     f'Exec=env PTTMAN_START_HIDDEN=1 "{binary}" gui\n'
                                     'Icon=audio-input-microphone\nTerminal=false\n')
            else:
                AUTOSTART.unlink(missing_ok=True)
            if running():
                send("reload")
            self.report("Settings saved. Startup mute takes effect next time control starts.")
            return True
        except (OSError, ValueError, IndexError) as error:
            self.report(str(error))
            return False

    def close_window(self, *_):
        self.edge("button", False)
        self.edge("key", False)
        # The global shortcut lives in this process, so hidden-icon mode keeps running too.
        if self.keep_in_tray.get_active() and (not self.tray or self.tray.active):
            self.window.set_visible(False)
            return True
        self.quit()
        return False

    def shutdown(self, *_):
        self.closing = True
        releasing = self.pending_release is not None
        if releasing:
            GLib.source_remove(self.pending_release)
            self.pending_release = None
        if self.held or releasing:
            self.action("release")
            self.held.clear()
        if self.portal:
            self.portal.close()
        if self.tray:
            self.tray.close()
        if self.preview and self.preview.poll() is None:
            self.preview.terminate()
        if self.daemon and self.daemon.poll() is None:
            # Muting is serialized before SIGTERM; direct mute also covers a busy daemon.
            try:
                send("mute")
                self.command([self.binary, "mute"])
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                pass
            self.daemon.terminate()
            try:
                self.daemon.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.daemon.kill()
                self.daemon.wait()
            try:
                self.command([self.binary, "mute"])
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                pass
        self.pool.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    app = App(sys.argv[1] if len(sys.argv) > 1 else shutil.which("pttman") or "pttman")
    raise SystemExit(app.run([sys.argv[0]]))
