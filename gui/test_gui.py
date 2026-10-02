import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pttman_gui as gui


class PlaybackTests(unittest.TestCase):
    def test_mp3_preview_uses_ffplay_case_insensitively(self):
        self.assertEqual(gui.playback_command("/sounds/push sound.MP3"),
                         ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", "-volume", "100", "-i", "/sounds/push sound.MP3"])
        self.assertEqual(gui.playback_command("/sounds/release.wav"),
                         ["paplay", "--volume=65536", "--", "/sounds/release.wav"])


class VolumeTests(unittest.TestCase):
    def test_volume_for_both_players_and_limits(self):
        self.assertIn("--volume=32768", gui.playback_command("cue.wav", 50))
        self.assertIn("--volume=0", gui.playback_command("cue.wav", 0))
        self.assertIn("--volume=65536", gui.playback_command("cue.wav", 100))
        self.assertEqual(gui.playback_command("cue.mp3", 35)[6], "35")


class TrayTests(unittest.TestCase):
    def test_actual_microphone_states(self):
        self.assertEqual(gui.microphone_state("source mic: unmuted *")[0], "live")
        self.assertEqual(gui.microphone_state("source mic: muted *\nsource other: unmuted")[0], "muted")
        self.assertEqual(gui.microphone_state("source mic: unknown *")[0], "unknown")
        self.assertEqual(gui.microphone_state("connection error")[0], "unknown")
        self.assertEqual(gui.microphone_state("source one: muted *\nsource two: unmuted *")[0], "live")

    def test_lamp_pixmaps_are_argb_at_all_sizes(self):
        for state in ("live", "muted", "unknown"):
            for width, height, pixels in gui.lamp_pixmaps(state):
                self.assertEqual(len(pixels), width * height * 4)
        self.assertNotEqual(gui.lamp_pixmaps("live"), gui.lamp_pixmaps("muted"))

    def test_menu_layout_has_valid_dbus_signature(self):
        tray = gui.Tray.__new__(gui.Tray)
        tray.items = {1: ("Open settings", lambda: None), 2: ("Mute", lambda: None)}
        layout = gui.GLib.Variant("(u(ia{sv}av))", (1, tray.layout(0, -1, [])))
        self.assertEqual(len(layout.unpack()[1][2]), 2)

    def test_all_timeout_formats_and_limits(self):
        for value in ("off", "0", "500ms", "120s", "2m", "24h", "42"):
            self.assertTrue(gui.validate_timeout(value))
        for value in ("-1", "0.5s", "bad", "999999999999999999999h", "2m\n--source=bad"):
            with self.assertRaises(ValueError):
                gui.validate_timeout(value)


class ConfigTests(unittest.TestCase):
    def test_save_preserves_comments_unrelated_flags_and_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "actual.conf"
            target.write_text("# personal settings\n--source=old\n--start-muted=false\n")
            target.chmod(0o640)
            link = Path(folder) / "pttman.conf"
            link.symlink_to(target)
            with patch.object(gui, "CONFIG", link):
                gui.save_config({"--source": "", "--all-sources": "true", "--press-sound": "/tmp/a b.wav"})
                self.assertEqual(gui.read_config()["--press-sound"], "/tmp/a b.wav")
                self.assertEqual(gui.read_config()["--start-muted"], "false")
                self.assertNotIn("--source", gui.read_config())
            self.assertTrue(link.is_symlink())
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)
            self.assertIn("# personal settings", target.read_text())

    def test_rejects_config_injection_before_writing(self):
        with self.assertRaises(ValueError):
            gui.save_config({"--press-sound": "test\n--start-muted=false"})


class InputTests(unittest.TestCase):
    def controller(self):
        app = Mock()
        app.held = set()
        app.pending_release = None
        app.action.return_value = True
        app.send_release = lambda: gui.App.send_release(app)
        return app

    def edges(self, app, edges):
        timers = []
        with patch.object(gui.GLib, "timeout_add", lambda delay, callback: timers.append(callback) or len(timers)), \
             patch.object(gui.GLib, "source_remove", lambda timer: timers.__setitem__(timer - 1, None)):
            for origin, pressed in edges:
                gui.App.edge(app, origin, pressed)
        for callback in timers:
            if callback:
                callback()

    def test_repeat_and_overlapping_inputs_have_one_press_and_release(self):
        app = self.controller()
        self.edges(app, [("global", True), ("global", True), ("button", True),
                         ("global", False), ("button", False), ("button", False)])
        self.assertEqual([call.args for call in app.action.call_args_list], [("press",), ("release",)])
        self.assertEqual(app.held, set())

    def test_release_chatter_inside_debounce_continues_the_hold(self):
        app = self.controller()
        self.edges(app, [("global", True), ("global", False), ("global", True), ("global", False)])
        self.assertEqual([call.args for call in app.action.call_args_list], [("press",), ("release",)])
        self.assertIsNone(app.pending_release)

    def test_failed_press_does_not_leave_input_held(self):
        app = self.controller()
        app.action.return_value = False
        gui.App.edge(app, "global", True)
        self.assertEqual(app.held, set())

    def test_global_release_is_filtered_by_session_and_id(self):
        portal = Mock(session="/session/ours")
        for session, name in [("/session/other", "talk"), ("/session/ours", "other"),
                              ("/session/ours", "talk")]:
            params = gui.GLib.Variant("(osta{sv})", (session, name, 0, {}))
            gui.GlobalShortcut.signal(portal, None, None, None, None, "Deactivated", params)
        portal.edge.assert_called_once_with("global", False)


if __name__ == "__main__":
    unittest.main()
