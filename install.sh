#!/usr/bin/env bash
# Electric Notifier installer
#   ./install.sh               install the boot autostart entry + run tests
#   ./install.sh --uninstall   remove the autostart entry (keeps history/state)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
AUTOSTART="$HOME/.config/autostart/electric-notifier.desktop"

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -f "$AUTOSTART"
  echo "Removed $AUTOSTART (history/state kept in ~/.local/share/electric-notifier)."
  exit 0
fi

command -v python3 >/dev/null 2>&1 || { echo "python3 is required"; exit 1; }
command -v notify-send >/dev/null 2>&1 || { echo "notify-send missing — run: sudo apt install libnotify-bin"; exit 1; }

mkdir -p "$(dirname "$AUTOSTART")"
cat > "$AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Name=Electric Notifier
Comment=Pops up power-outage / billing updates from your electric provider at login
Exec=/usr/bin/python3 "$HERE/electric-notifier.py" boot
Terminal=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
Categories=Utility;
EOF

echo "Installed autostart entry: $AUTOSTART"
echo
echo "== popup test (you should see a notification) =="
python3 "$HERE/electric-notifier.py" test-notify || true

echo
echo "== feed test (needs the RSSHub setup from README.md) =="
python3 "$HERE/electric-notifier.py" test-feed || true

echo
echo "== first-run bootstrap =="
python3 "$HERE/electric-notifier.py" boot
echo "(existing posts were marked as seen — from now on you only get NEW outage/billing posts)"
