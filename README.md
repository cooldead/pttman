# pttman

Reliable push-to-talk and mic-mute for PipeWire.

## Desktop GUI (local fork)

This fork adds a GTK 4 desktop window and optional sounds when push-to-talk
begins and ends. The Rust binary embeds the GUI script; it needs Python 3, PyGObject,
and GTK 4 at runtime. Sound playback needs `paplay` for WAV/OGG/FLAC and `ffplay` (FFmpeg) for MP3.
On Arch/CachyOS these are provided by `python`, `python-gobject`, `gtk4`, and
`libpulse`; install `ffmpeg` for MP3.

```sh
cargo build --release
./run-gui.sh
# Or run the binary directly:
./target/release/pttman gui
```

1. Choose a microphone or **All microphones**.
2. Optionally enable the **Press sound** and **Release sound** options, select an MP3, WAV,
   OGG, or FLAC file, and preview it. Use a short sound; it plays through the
   default audio output and may be picked up by your microphone on speakers.
3. Adjust **Sound volume** (0–100%) for both cues and previews, then save settings. Set a maximum talk time such as `120s` to recover from missed key releases,
   or use `off` to disable the timeout.
4. Click **Start control** if the daemon is stopped.
5. Click **Configure global shortcut…** and choose a key in the desktop dialog
   (F9 is suggested). Hold it to talk while another application is focused.

Global shortcuts use the XDG GlobalShortcuts portal, including its activation
and deactivation signals. KDE Plasma supports this on Wayland. Other desktops
need a compatible portal backend. Mouse buttons can be mapped to the chosen
key using mouse software; the GUI does not capture raw mouse devices.
The desktop remembers the binding, and the GUI reconnects on subsequent
launches. The global shortcut stays active when the window is hidden in the tray.
Existing external `pttman press` / `pttman release` bindings still work without
the GUI.

The window also has a hold-to-talk button and explicit mute/unmute controls.
Losing window focus releases the GUI button without cancelling an active
global shortcut. Closing the window hides it in the tray by default and keeps the global shortcut
active. Quit from the tray menu to exit and release any hold. If the GUI started
the daemon, it mutes and stops that daemon on exit; an existing service keeps
running. **Save settings** reloads the running daemon.

Use this fork's binary for both the GUI and the daemon: the upstream daemon
does not recognize the new sound setting. A service installed before this fork
must be updated to run the new binary and restarted before saving GUI settings.

The sound setting in `pttman.conf` is:

```text
--press-sound=/absolute/path/to/press.wav
--release-sound=/absolute/path/to/release.wav
--sound-volume=100
```

Set either sound to `off` to disable it (the default). The daemon plays the sound
after successfully applying a new press, without waiting for playback. Repeated
key-down events do not retrigger it. The release sound plays after a successful
mute at the end of a hold; duplicate releases stay silent. Each sound has its
own player so releasing quickly still plays the release cue. Overlapping
instances of the same cue are suppressed, and
playback failures do not block muting. Sound requires the daemon; the CLI's
direct fallback does not play it.

### Tray and configuration

The KDE tray icon is a caged red warning light. It glows red when any managed
microphone is unmuted, stays dark red when all managed microphones are muted,
and turns amber if the state cannot be read. It reads actual microphone state
about every 300 ms, including changes made outside the GUI. Hover for status,
click to open settings, or right-click for mute, unmute, toggle, resync, and quit.

The GUI exposes every daemon setting: a specific microphone, all microphones,
or the system default; startup mute; timeout in milliseconds/seconds/minutes/hours
or `off`; both sound files; and cue volume. It also includes systemd service
start/stop/restart, install/uninstall, and enable/disable at login. Service controls
are for systemd desktops; the existing CLI still supports OpenRC.

**Desktop** settings control close-to-tray and starting the GUI at login. Login
startup opens directly in the tray when a tray host is available. The tray uses
KDE's StatusNotifierItem protocol and a D-Bus menu, without extra Python packages.
If no tray host is available, the window remains accessible.

Additional GUI checks:

```sh
python3 -m unittest discover -s gui -p 'test_*.py'
python3 gui/smoke_test.py # needs a desktop session; briefly opens a window
```

`pttman` is a small user service that keeps microphone mute state predictable:

- Rapid mute, unmute, and toggle key presses are serialized through a Unix
  datagram socket, so quick press-release cycles cannot race into the wrong
  state.
- The intended mute state is reapplied after PipeWire source changes.
- Accidental unmutes from other tools are reverted quickly.
- When auto-discovering sources, the source list is also re-checked every few
  seconds, so events lost during an audio-stack restart cannot leave the
  daemon managing a stale source set.

## Requirements

- PipeWire with PulseAudio compatibility or PulseAudio
- `pactl`
- One of: systemd or OpenRC

## Installation

### cargo

```bash
cargo install pttman
pttman install-service
```

Start the service:

```bash
systemctl --user start pttman.service          # systemd
rc-service --user pttman start                 # OpenRC 0.60+
sudo rc-service pttman start                   # older OpenRC
```

### Prebuilt binary

Download the tarball for your platform from the
[GitHub releases page](https://github.com/mwolson/pttman/releases), extract it,
and move `pttman` to `~/.local/bin/` or `/usr/local/bin/`. Then run:

```bash
pttman install-service
```

### install.sh (systemd, source build)

```bash
git clone https://github.com/mwolson/pttman.git
cd pttman
./install.sh
systemctl --user start pttman.service
```

`install.sh` runs `cargo build --release`, copies the binary to
`~/.local/bin/`, and calls `pttman install-service`.

## Commands

```text
pttman                                 Run the daemon (default)
pttman get-default-source              Print the default source from the config file
pttman install-service                 Install and enable the service (systemd or OpenRC)
pttman list-sources                    List available audio sources
pttman mute                            Mute the microphone and record it as the preference
pttman press                           Temporarily unmute (push-to-talk, does not change preference)
pttman release                         Temporarily mute (push-to-talk, does not change preference)
pttman resync                          Ask the daemon to reapply its desired mute state
pttman set-default-source SOURCE       Save default source and signal the daemon
pttman status                          Print the current microphone state
pttman toggle                          Toggle the microphone mute state and record the new state as the preference
pttman uninstall-service               Disable and remove the service (systemd or OpenRC)
pttman unmute                          Unmute the microphone and record it as the preference
```

### Options

These flags apply to the daemon and action commands:

```text
--source SOURCE     Audio source name to control (default: config file, then all sources)
--all-sources       Operate on all audio sources (overrides --source from config)
--start-muted       Mute managed sources when the daemon starts (default: true)
--no-start-muted    Leave mic state untouched when the daemon starts
```

## Configuration File

`pttman` reads defaults from `~/.config/pttman.conf` or
`$XDG_CONFIG_HOME/pttman.conf`. The file uses one flag per line:

```text
--source=alsa_input.usb-046d_BRIO-03.pro-input-0
--ptt-hold-timeout=2m
```

Supported flags:

- `--source=NAME` controls only this source
- `--all-sources=true` controls all sources
- `--ptt-hold-timeout=off|DURATION` sets the maximum time a `press` command may
  keep the mic unmuted before the daemon mutes it. It defaults to `off`.
  Durations accept `ms`, `s`, `m`, and `h` suffixes. Bare numbers are seconds.
- `--start-muted=true|false` controls whether the daemon mutes managed sources
  at startup

`--source` and `--all-sources=true` are mutually exclusive. Command-line
arguments always take precedence over the config file. `pttman
set-default-source` removes any `--all-sources` line when it writes
`--source`, keeping the config valid.

## Push-to-talk bindings

Use `pttman press` on key down and `pttman release` on key up:

```yaml
F5:
  skip_key_event: true
  press: { launch: ["pttman", "press"] }
  release: { launch: ["pttman", "release"] }
```

For missed key-up events, add a timeout such as `--ptt-hold-timeout=2m` to the
config file.

## Development

```bash
bun run test
bun run hooks:check
```

## License

MIT
