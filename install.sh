#!/usr/bin/env bash
# Installs xfce-overview for the current user (no root needed).
#   ./install.sh            install
#   ./install.sh --remove   uninstall
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"

if [[ "${1:-}" == "--remove" ]]; then
    "$BIN/xfce-overview" --quit 2>/dev/null || true
    rm -f "$BIN/xfce-overview" "$BIN/xfce-overview-toggle" "$AUTOSTART/xfce-overview.desktop"
    echo "Removed."
    exit 0
fi

mkdir -p "$BIN" "$AUTOSTART"

install -m 755 "$HERE/xfce-overview.py" "$BIN/xfce-overview"

# Fast trigger for the keyboard shortcut: a raw D-Bus call (no Python start-up,
# so it reacts instantly). Falls back to starting the daemon if it isn't running.
cat > "$BIN/xfce-overview-toggle" <<'EOF'
#!/bin/sh
gdbus call --session \
    --dest io.github.local.XfceOverview \
    --object-path /io/github/local/XfceOverview \
    --method org.freedesktop.Application.Activate '{}' >/dev/null 2>&1 \
  || { setsid "$HOME/.local/bin/xfce-overview" >/dev/null 2>&1 & }
EOF
chmod 755 "$BIN/xfce-overview-toggle"

# Autostart the (hidden) daemon at login
cat > "$AUTOSTART/xfce-overview.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=XFCE Overview
Comment=GNOME-style activities overview (background daemon)
Exec=$BIN/xfce-overview --daemon
Terminal=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
EOF

echo "Installed to $BIN"
echo "Start it now with:   $BIN/xfce-overview --daemon &"
echo "Shortcut command:    $BIN/xfce-overview-toggle"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "Note: $BIN is not in your PATH (shortcut/autostart use absolute paths, so that's fine).";; esac
