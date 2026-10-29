#!/usr/bin/env bash
# Electric Notifier — one-command recovery after an OS reinstall/wipe.
# Asks for your Facebook cookies at runtime (xs input is hidden; the script
# itself never stores them). Safe to keep in a public repo.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "Electric Notifier — restore"
echo "---------------------------"

command -v docker >/dev/null 2>&1 || {
  echo "Docker not found. Install Docker Desktop first, then re-run this script."
  exit 1
}

read -rp "c_user cookie value : " CUSER
read -rsp "xs    cookie value (hidden): " XS; echo
if [[ -z "${CUSER// }" || -z "${XS// }" ]]; then
  echo "Both cookie values are required."
  exit 1
fi

docker rm -f rsshub 2>/dev/null || true
docker run -d --name rsshub --restart=always \
  -p 127.0.0.1:1200:1200 \
  -e FACEBOOK_COOKIE="c_user=${CUSER}; xs=${XS}" \
  diygod/rsshub

# user systemd: docker app + background watcher
mkdir -p "$HOME/.config/systemd/user"
cp "$HERE/systemd/electric-notifier.service" "$HERE/systemd/electric-notifier.timer" \
   "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable docker-desktop 2>/dev/null || true
systemctl --user enable --now electric-notifier.timer

# autostart popup at login + test run
"$HERE/install.sh"
