#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════
# Casa Volterra TV — Setup OSD Volume (wob) v1.2
# Fix: pkill -x wob (non -f che killava lo script stesso)
#      wob avviato con sleep 5 nell'autostart (attende compositor)
# ══════════════════════════════════════════════════════════════

# NON usare set -e: i pkill restituiscono 1 se non trovano nulla
set -uo pipefail

WOB_PIPE="/tmp/cv-tv-wob.sock"
WOB_LOG="/tmp/cv-tv-wob.log"
AUTOSTART="$HOME/.config/labwc/autostart"

# ── Variabili Wayland ──────────────────────────────────────────
CONFIG_FILE="$HOME/cv-tv-config.json"
WL_DISPLAY="wayland-0"
XDG_RT="/run/user/1000"
if [[ -f "$CONFIG_FILE" ]]; then
  WL_DISPLAY="$(python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c['proxy'].get('wayland_display','wayland-0'))")"
  XDG_RT="$(python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c['proxy'].get('xdg_runtime_dir','/run/user/1000'))")"
fi

export WAYLAND_DISPLAY="$WL_DISPLAY"
export XDG_RUNTIME_DIR="$XDG_RT"
export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RT}/bus"
export XDG_SESSION_TYPE="wayland"

echo "=== Ambiente Wayland ==="
echo "  WAYLAND_DISPLAY=$WAYLAND_DISPLAY"
echo "  XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"
echo "  Socket Wayland: $(test -S "${XDG_RT}/${WL_DISPLAY}" && echo 'esiste' || echo 'NON trovato')"

# ── wob installato? ────────────────────────────────────────────
if ! command -v wob &>/dev/null; then
  echo ""
  echo "=== Installo wob ==="
  sudo apt-get install -y wob
fi
echo ""
echo "=== wob: $(wob --version 2>/dev/null || echo 'installato') ==="

# ── Ferma wob esistente (usa -x = exact match, non -f!) ────────
echo ""
echo "=== Pulizia processi wob esistenti ==="
pkill -x wob 2>/dev/null && echo "  wob killato" || echo "  nessun wob in esecuzione"
pkill -f "tail -f /tmp/cv-tv-wob.sock" 2>/dev/null || true
sleep 1

# ── Crea named pipe ────────────────────────────────────────────
rm -f "$WOB_PIPE"
mkfifo "$WOB_PIPE"
echo "  Pipe creata: $WOB_PIPE OK"

# ── Avvia wob ──────────────────────────────────────────────────
echo ""
echo "=== Avvio wob ==="

tail -f "$WOB_PIPE" | \
  WAYLAND_DISPLAY="$WL_DISPLAY" \
  XDG_RUNTIME_DIR="$XDG_RT" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RT}/bus" \
  XDG_SESSION_TYPE="wayland" \
  wob \
    --anchor bottom \
    --margin 80 \
    --width 400 \
    --height 50 \
    --border 4 \
    --padding 8 \
    --background-color 1A1A2ECC \
    --border-color 3B82F6FF \
    --bar-color 3B82F6FF \
    --overflow-background-color 7F1D1DCC \
    --overflow-bar-color EF4444FF \
    --timeout 2000 \
  >> "$WOB_LOG" 2>&1 &

WOB_BG_PID=$!
sleep 2

if kill -0 "$WOB_BG_PID" 2>/dev/null; then
  echo "  wob pipeline avviata PID=$WOB_BG_PID OK"
  WOB_OK=true
else
  WOB_OK=false
  echo "  wob crashato. Log:"
  cat "$WOB_LOG"
fi

# ── Test visivo ────────────────────────────────────────────────
if [[ "$WOB_OK" == true ]]; then
  echo ""
  echo "=== Test visivo (barra visibile sulla TV) ==="
  for pct in 30 60 90 50; do
    printf "  %d%%... " "$pct"
    if timeout 2 bash -c "echo $pct > $WOB_PIPE" 2>/dev/null; then
      echo "OK"
    else
      echo "timeout"
    fi
    sleep 1
  done
fi

# ── Aggiorna autostart labwc ───────────────────────────────────
echo ""
echo "=== Aggiorno autostart labwc ==="

if grep -q "wob" "$AUTOSTART" 2>/dev/null; then
  python3 - "$AUTOSTART" << 'PYEOF'
import sys, re
content = open(sys.argv[1]).read()
cleaned = re.sub(r'\n# ── OSD Volume.*?--timeout \d+.*?> /tmp/cv-tv-wob\.log 2>&1\n\) &\n', '\n', content, flags=re.DOTALL)
open(sys.argv[1], 'w').write(cleaned)
print("  Rimosso blocco wob precedente")
PYEOF
fi

cat >> "$AUTOSTART" << 'BLOCK_EOF'

# ── OSD Volume (wob) ──────────────────────────────────────────
# sleep 5: attende che compositor e Chromium siano pronti
(
  sleep 5
  pkill -x wob 2>/dev/null || true
  rm -f /tmp/cv-tv-wob.sock && mkfifo /tmp/cv-tv-wob.sock
  tail -f /tmp/cv-tv-wob.sock | \
    wob \
      --anchor bottom \
      --margin 80 \
      --width 400 \
      --height 50 \
      --border 4 \
      --padding 8 \
      --background-color 1A1A2ECC \
      --border-color 3B82F6FF \
      --bar-color 3B82F6FF \
      --overflow-background-color 7F1D1DCC \
      --overflow-bar-color EF4444FF \
      --timeout 2000 \
    >> /tmp/cv-tv-wob.log 2>&1
) &
BLOCK_EOF

echo "  Autostart aggiornato OK"

echo ""
echo "======================================================"
echo "Stato: wob gira: $(pgrep -x wob >/dev/null && echo 'SI' || echo 'NO - serve riavvio')"
echo "Test manuale: echo 70 > /tmp/cv-tv-wob.sock"
echo "Log wob:      cat /tmp/cv-tv-wob.log"
echo ""
echo "Se la barra non appare: sudo reboot"
echo "======================================================"
