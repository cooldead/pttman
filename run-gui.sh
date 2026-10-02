#!/bin/bash
set -euo pipefail

for dependency in python3 pactl; do
    if ! command -v "$dependency" >/dev/null 2>&1; then
        echo "Missing dependency: $dependency" >&2
        exit 1
    fi
done
if ! python3 -c 'import gi; gi.require_version("Gtk", "4.0"); from gi.repository import Gtk' 2>/dev/null; then
    echo "Install Python GObject and GTK 4 (Arch/CachyOS: python-gobject gtk4)." >&2
    exit 1
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -x "$SCRIPT_DIR/target/release/pttman" ]]; then
    echo "Build first: cargo build --release" >&2
    exit 1
fi
exec "$SCRIPT_DIR/target/release/pttman" gui
