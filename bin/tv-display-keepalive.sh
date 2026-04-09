#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════
# Casa Volterra TV — Display Keepalive v1.2
#
# v1.2:
#   - TRIGGER SOCKET: il proxy Python può scrivere su
#     /tmp/cv-tv-cec-trigger per forzare un "on 0" CEC immediato.
#     Risolve il problema "TV si spegne all'apertura del browser"
#     causato da wf-recorder kill → micro-blackout HDMI.
#   - Intervallo ping ridotto a 30s (era 55s) per recovery più rapido
#   - cec-client con timeout 5s (evita blocchi)
#   - DPMS re-disable ogni 5 minuti (non solo all'avvio)
# ══════════════════════════════════════════════════════════════

set -u

WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/1000}"
LOG_FILE="/tmp/cv-tv-display-keepalive.log"
PING_INTERVAL=30        # secondi tra un ping CEC e l'altro (ridotto da 55)
DPMS_RECHECK=300        # ri-disabilita DPMS ogni 5 minuti

# Socket file: il proxy scrive qui per forzare CEC "on 0" immediato
TRIGGER_FILE="/tmp/cv-tv-cec-trigger"

export WAYLAND_DISPLAY XDG_RUNTIME_DIR
export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"
export XDG_SESSION_TYPE=wayland

log() {
  local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
  echo "$msg"
  echo "$msg" >> "$LOG_FILE"
}

# ── Leggi config ───────────────────────────────────────────────
CONFIG_FILE="${HOME}/cv-tv-config.json"
if [[ -f "$CONFIG_FILE" ]]; then
  WAYLAND_DISPLAY="$(python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c['proxy'].get('wayland_display','wayland-0'))" 2>/dev/null || echo "wayland-0")"
  XDG_RUNTIME_DIR="$(python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c['proxy'].get('xdg_runtime_dir','/run/user/1000'))" 2>/dev/null || echo "/run/user/1000")"
  export WAYLAND_DISPLAY XDG_RUNTIME_DIR
  export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"
fi

# ── Disabilita DPMS ────────────────────────────────────────────
disable_dpms() {
  log "Disabilito DPMS..."

  if command -v wlr-randr &>/dev/null; then
    local output
    output="$(wlr-randr 2>/dev/null | grep -v '^\s' | head -1 | awk '{print $1}')"
    if [[ -n "$output" ]]; then
      wlr-randr --output "$output" --on 2>/dev/null && log "wlr-randr --on $output OK" || true
    fi
  fi

  if command -v wlopm &>/dev/null; then
    wlopm --on '*' 2>/dev/null && log "wlopm --on OK" || true
  fi

  if command -v xset &>/dev/null; then
    DISPLAY=":0" xset s off -dpms 2>/dev/null && log "xset dpms off OK" || true
  fi
}

# ── Comando CEC ────────────────────────────────────────────────
cec_on() {
  # "on 0" = accendi TV (address 0 = TV)
  if command -v cec-client &>/dev/null; then
    echo "on 0" | timeout 5 cec-client -s -d 1 2>/dev/null | grep -v "^$" | tail -1 || true
    log "CEC: on 0 inviato"
  fi
}

cec_active_source() {
  # "as" = set active source → conferma segnale HDMI attivo
  if command -v cec-client &>/dev/null; then
    echo "as" | timeout 5 cec-client -s -d 1 2>/dev/null | grep -v "^$" | tail -1 || true
  fi
}

# ── Controlla trigger file ─────────────────────────────────────
check_trigger() {
  if [[ -f "$TRIGGER_FILE" ]]; then
    local reason
    reason="$(cat "$TRIGGER_FILE" 2>/dev/null || echo "unknown")"
    rm -f "$TRIGGER_FILE"
    log "⚡ Trigger CEC ricevuto (motivo: $reason) — invio on 0"
    # Prima riabilita display Wayland, poi manda CEC
    disable_dpms
    sleep 1
    cec_on
    sleep 2
    cec_active_source
  fi
}

# ══════════════════════════════════════════════════════════════
: > "$LOG_FILE"
log "Display Keepalive v1.2 avviato (ping ogni ${PING_INTERVAL}s, trigger=${TRIGGER_FILE})"

sleep 8
disable_dpms
sleep 2
cec_on

last_dpms_check=$(date +%s)

# Loop principale — polling ogni 3s per il trigger file
while true; do
  local_now=$(date +%s)

  # Controlla trigger immediato dal proxy
  check_trigger

  # Ping CEC periodico
  if (( local_now - ${last_ping_time:-0} >= PING_INTERVAL )); then
    cec_active_source
    last_ping_time=$local_now
  fi

  # Re-disabilita DPMS periodicamente
  if (( local_now - last_dpms_check >= DPMS_RECHECK )); then
    disable_dpms
    last_dpms_check=$local_now
  fi

  sleep 3
done
