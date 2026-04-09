#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════
# Casa Volterra TV — Launcher manuale
#
# USO MANUALE SOLO (debug/test). In produzione usa il watchdog.
# Legge TV_URL (con token) da ~/cv-tv-config.json.
# ══════════════════════════════════════════════════════════════

CONFIG_FILE="${HOME}/cv-tv-config.json"

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "ERRORE: $CONFIG_FILE non trovato" >&2
  exit 1
fi

TV_URL="$(python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c['proxy'].get('tv_url',''))" 2>/dev/null)"
WAYLAND_VAL="$(python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c['proxy'].get('wayland_display','wayland-0'))" 2>/dev/null)"
XDG_VAL="$(python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c['proxy'].get('xdg_runtime_dir','/run/user/1000'))" 2>/dev/null)"

if [[ -z "${TV_URL:-}" ]]; then
  echo "ERRORE: tv_url non trovato in $CONFIG_FILE" >&2
  exit 1
fi

echo "Fermando Chromium esistente..."
pkill -f "chromium.*casa-volterra\.it" 2>/dev/null || true
pkill -f "cv_token=" 2>/dev/null || true
sleep 1

export WAYLAND_DISPLAY="${WAYLAND_VAL:-wayland-0}"
export XDG_RUNTIME_DIR="${XDG_VAL:-/run/user/1000}"
export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"
export XDG_SESSION_TYPE=wayland
export GDK_BACKEND=wayland

echo "Avvio Chromium → $TV_URL"
chromium \
  --kiosk \
  --noerrdialogs \
  --disable-infobars \
  --no-first-run \
  --disable-session-crashed-bubble \
  --no-default-browser-check \
  --autoplay-policy=no-user-gesture-required \
  --disable-features=PrivateNetworkAccessChecks,BlockInsecurePrivateNetworkRequests \
  --allow-running-insecure-content \
  --check-for-update-interval=31536000 \
  --password-store=basic \
  --disk-cache-size=1 \
  --app="$TV_URL" \
  > /tmp/cv-tv-chromium.log 2>&1 &

echo "Chromium PID=$! — log: /tmp/cv-tv-chromium.log"
