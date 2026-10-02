"""Check real portal session creation without registering a shortcut."""
from gi.repository import GLib
from pttman_gui import GlobalShortcut

loop = GLib.MainLoop()
results = []


def report(message):
    if "unavailable" in message or "cancelled" in message:
        results.append(message)
        loop.quit()


def created(data):
    portal.session = data["session_handle"]
    print("Portal created a global-shortcut session successfully")
    portal.close()
    results.append("ok")
    loop.quit()


def timeout():
    results.append("Portal session timed out")
    loop.quit()
    return False


portal = GlobalShortcut(lambda *args: None, report, lambda: None)
portal.created = created
portal.setup()
GLib.timeout_add_seconds(10, timeout)
loop.run()
if results != ["ok"]:
    raise SystemExit(str(results))
