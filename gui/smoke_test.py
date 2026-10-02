"""Open the GUI briefly and check the real desktop, without changing mic state."""
from pathlib import Path
import sys
from gi.repository import Gio, GLib
import pttman_gui as gui
from pttman_gui import App, PORTAL, OBJECT, SHORTCUTS
import tempfile

folder = tempfile.TemporaryDirectory()
gui.SHORTCUT_ENABLED = Path(folder.name) / "no-shortcut"

app = App(str(Path(__file__).resolve().parents[1] / "target/debug/pttman"))
app.set_application_id("io.github.mwolson.pttman.smoketest")
errors = []
menu_checked = []


def finish():
    try:
        assert menu_checked == [True], "Tray menu D-Bus call failed"
        assert app.window.get_visible()
        assert app.sources.get_model().get_n_items() > 0
        assert app.tray.active, "KDE did not register the tray icon"
        assert app.timeout.get_text()
        app.window.close()
        assert not app.window.get_visible(), "Close did not hide to tray"
        app.activate()
        assert app.window.get_visible(), "Tray activation did not reopen window"
        print("Tray registration and hide/reopen passed")
        print("GUI status:", app.status.get_text())
        print("Microphone choices:", app.sources.get_model().get_n_items())
        print("GUI message:", app.message.get_text())
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(PORTAL, OBJECT, "org.freedesktop.DBus.Properties", "Get",
                              GLib.Variant("(ss)", (SHORTCUTS, "version")),
                              None, Gio.DBusCallFlags.NONE, 3000, None)
        print("GlobalShortcuts portal version:", reply.unpack()[0])
    except Exception as error:
        errors.append(str(error))
    finally:
        app.quit()
    return False


def check_menu():
    def work():
        return app.command(["gdbus", "call", "--session", "--dest", app.tray.bus.get_unique_name(),
                            "--object-path", "/Menu", "--method", "com.canonical.dbusmenu.GetLayout",
                            "0", "1", "[]"])
    def done(text):
        assert "Mute microphone" in text and "Open settings" in text
        menu_checked.append(True)
    app.background(work, done)
    return False


GLib.timeout_add(1000, check_menu)
GLib.timeout_add(3200, finish)
app.run([sys.argv[0]])
if errors:
    raise SystemExit("; ".join(errors))
print("GUI smoke test passed")
