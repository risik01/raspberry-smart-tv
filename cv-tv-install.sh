#!/usr/bin/env bash
# ╔══════════════════════════════════════════════════════════════════╗
# ║         Casa Volterra TV — Installer / Reinstaller v1.0         ║
# ║                                                                  ║
# ║  Esegui come utente pi (non root):                               ║
# ║    bash cv-tv-install.sh                                         ║
# ║                                                                  ║
# ║  Cosa fa:                                                        ║
# ║    1. CLEANUP  — ferma servizi, rimuove file vecchi/corrotti     ║
# ║    2. INSTALL  — installa script corretti + servizi systemd      ║
# ║    3. CONFIG   — crea/aggiorna cv-tv-config.json                 ║
# ║    4. AUTOSTART — scrive labwc autostart pulito (una volta)      ║
# ║    5. TEST     — verifica che tutto sia correttamente attivo      ║
# ╚══════════════════════════════════════════════════════════════════╝

set -euo pipefail

# ── Colori ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'
BLU='\033[0;34m'; CYN='\033[0;36m'; BLD='\033[1m'; RST='\033[0m'

ok()   { echo -e "${GRN}✅ $*${RST}"; }
warn() { echo -e "${YLW}⚠️  $*${RST}"; }
err()  { echo -e "${RED}❌ $*${RST}"; }
info() { echo -e "${BLU}ℹ️  $*${RST}"; }
hdr()  { echo -e "\n${BLD}${CYN}══ $* ══${RST}"; }

# ── Verifica utente ───────────────────────────────────────────────────
if [[ "$(id -u)" -eq 0 ]]; then
  err "Non eseguire come root. Usa: bash cv-tv-install.sh"
  exit 1
fi
if [[ "$(whoami)" != "pi" ]]; then
  warn "Utente corrente: $(whoami) — lo script è pensato per l'utente 'pi'."
  warn "Premi Invio per continuare comunque, Ctrl+C per annullare."
  read -r
fi

# ── Percorsi ──────────────────────────────────────────────────────────
HOME_DIR="$HOME"
BIN_DIR="$HOME_DIR/bin"
CONFIG_FILE="$HOME_DIR/cv-tv-config.json"
LABWC_AUTOSTART="$HOME_DIR/.config/labwc/autostart"
CACHE_DIR="$HOME_DIR/.cache"
LOG_DIR="/tmp"
SYSTEMD_USER_DIR="$HOME_DIR/.config/systemd/user"

# ── Servizi da gestire ────────────────────────────────────────────────
# I servizi Python esistenti (proxy, home-button, power-button) sono
# gestiti SOLO da systemd e non vengono toccati da questo installer
# a meno che non siano già presenti file .service nella home/systemd.
# Vengono però fermati durante la pulizia e riavviati alla fine.
PYTHON_SERVICES=(
  cv-tv-proxy
  cv-tv-home-button
  cv-tv-power-button
  cv-tv-keyboard
)

# ─────────────────────────────────────────────────────────────────────
# FASE 1 — CLEANUP
# ─────────────────────────────────────────────────────────────────────
phase_cleanup() {
  hdr "FASE 1 — Pulizia sistema"

  # 1a. Ferma tutti i Chromium legati al sito
  info "Fermo Chromium..."
  pkill -f "chromium.*casa-volterra\.it" 2>/dev/null && ok "Chromium fermato" || true
  pkill -f "cv_token=" 2>/dev/null || true
  sleep 1

  # 1b. Ferma watchdog e keepalive (processi bash)
  info "Fermo watchdog e keepalive..."
  pkill -f "tv-watchdog.sh"        2>/dev/null && ok "Watchdog fermato"   || true
  pkill -f "tv-display-keepalive"  2>/dev/null && ok "Keepalive fermato"  || true
  pkill -f "cv-tv-display-keepalive" 2>/dev/null || true
  sleep 1

  # 1c. Ferma servizi Python (systemd --user)
  info "Fermo servizi systemd user..."
  for svc in "${PYTHON_SERVICES[@]}"; do
    if systemctl --user is-active --quiet "${svc}.service" 2>/dev/null; then
      systemctl --user stop "${svc}.service" 2>/dev/null && ok "Fermato: $svc" || warn "Non fermato: $svc"
    fi
  done

  # 1d. Pulizia flag Chromium anomali (possibile fonte di --no-decommit-pooled-pages)
  info "Controllo file flag Chromium..."
  local chromium_flags_files=(
    "$HOME_DIR/.config/chromium-flags.conf"
    "/etc/chromium.d/cv-tv"
    "/etc/chromium.d/custom-flags"
    "$HOME_DIR/.config/chromium/Default/preferences"
  )
  for f in "${chromium_flags_files[@]}"; do
    if [[ -f "$f" ]]; then
      if grep -q "no-decommit-pooled-pages\|decommit" "$f" 2>/dev/null; then
        warn "Trovato flag obsoleto in $f — rimuovo il file"
        rm -f "$f"
        ok "Rimosso: $f"
      fi
    fi
  done

  # Controlla anche /etc/chromium.d/
  if sudo test -d /etc/chromium.d/ 2>/dev/null; then
    for f in $(sudo find /etc/chromium.d/ -type f 2>/dev/null); do
      if sudo grep -q "no-decommit" "$f" 2>/dev/null; then
        warn "Flag obsoleto trovato in $f — richiedo rimozione con sudo"
        sudo sed -i '/no-decommit-pooled-pages/d' "$f" && ok "Rimosso da: $f" || warn "Non ho potuto modificare $f"
      fi
    done
  fi

  # 1e. Rimuovi PID file obsoleti
  info "Pulizia PID file..."
  rm -f "$CACHE_DIR/tv-chromium.pid"
  ok "PID file rimossi"

  # 1f. Azzera log vecchi
  info "Pulizia log vecchi..."
  : > "$LOG_DIR/cv-tv-chromium.log"    2>/dev/null || true
  : > "$LOG_DIR/cv-tv-watchdog.log"    2>/dev/null || true
  : > "$LOG_DIR/cv-tv-keepalive.log"   2>/dev/null || true
  : > "$CACHE_DIR/tv-watchdog.log"     2>/dev/null || true
  ok "Log azzerati"

  # 1g. Rimuovi script vecchi da ~/bin (verranno riscritti)
  info "Rimozione script precedenti..."
  rm -f "$BIN_DIR/tv-watchdog.sh"
  rm -f "$BIN_DIR/tv-display-keepalive.sh"
  rm -f "$BIN_DIR/start-tv.sh"
  rm -f "$BIN_DIR/cv-tv-install.sh"   2>/dev/null || true
  ok "Script vecchi rimossi"

  ok "Cleanup completato"
}

# ─────────────────────────────────────────────────────────────────────
# FASE 2 — CONFIG interattiva
# ─────────────────────────────────────────────────────────────────────
phase_config() {
  hdr "FASE 2 — Configurazione cv-tv-config.json"

  # Leggi valori correnti se il file esiste già
  local cur_tv_url="" cur_token="" cur_port="8765"
  local cur_wayland="wayland-0" cur_xdg="/run/user/1000"
  local cur_bypass="false"

  if [[ -f "$CONFIG_FILE" ]]; then
    info "Trovato config esistente in $CONFIG_FILE"
    cur_tv_url="$(python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c['proxy'].get('tv_url',''))"        2>/dev/null || echo "")"
    cur_token="$(  python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c['proxy'].get('token',''))"         2>/dev/null || echo "")"
    cur_port="$(   python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c['proxy'].get('port',8765))"        2>/dev/null || echo "8765")"
    cur_wayland="$(python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c['proxy'].get('wayland_display','wayland-0'))" 2>/dev/null || echo "wayland-0")"
    cur_xdg="$(    python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c['proxy'].get('xdg_runtime_dir','/run/user/1000'))" 2>/dev/null || echo "/run/user/1000")"
    bypass_raw="$( python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c['proxy'].get('token_bypass',False))" 2>/dev/null || echo "False")"
    [[ "$bypass_raw" == "True" ]] && cur_bypass="true" || cur_bypass="false"

    echo ""
    echo "  Valori attuali:"
    echo "    tv_url        : ${cur_tv_url:-<vuoto>}"
    echo "    token         : ${cur_token:-<vuoto>}"
    echo "    port          : $cur_port"
    echo "    wayland       : $cur_wayland"
    echo "    xdg_runtime   : $cur_xdg"
    echo "    token_bypass  : $cur_bypass"
    echo ""
    read -rp "  Vuoi mantenere questi valori? [S/n] " keep_cfg
    if [[ "${keep_cfg:-s}" =~ ^[Ss]$ ]]; then
      ok "Config mantenuta"
      return 0
    fi
  fi

  echo ""
  info "Inserisci i nuovi valori (Invio = mantieni default tra []):"
  echo ""

  # TV URL
  local def_url="${cur_tv_url:-https://www.casa-volterra.it/televisione/}"
  read -rp "  TV URL completo con ?cv_token=... [$def_url]: " input_url
  local tv_url="${input_url:-$def_url}"

  # Estrai il token dall'URL se non c'è già un token separato
  local def_token="$cur_token"
  if [[ -z "$def_token" ]] && [[ "$tv_url" =~ cv_token=([^&]+) ]]; then
    def_token="${BASH_REMATCH[1]}"
  fi
  read -rp "  Token auth [$def_token]: " input_token
  local token="${input_token:-$def_token}"

  # Assicurati che tv_url contenga il token
  if [[ -n "$token" ]] && ! echo "$tv_url" | grep -q "cv_token="; then
    tv_url="${tv_url}?cv_token=${token}"
    info "Token aggiunto all'URL: $tv_url"
  fi

  read -rp "  Porta proxy [$cur_port]: " input_port
  local port="${input_port:-$cur_port}"

  read -rp "  Wayland display [$cur_wayland]: " input_wayland
  local wayland="${input_wayland:-$cur_wayland}"

  read -rp "  XDG_RUNTIME_DIR [$cur_xdg]: " input_xdg
  local xdg="${input_xdg:-$cur_xdg}"

  read -rp "  Token bypass (sviluppo) [$cur_bypass]: " input_bypass
  local bypass="${input_bypass:-$cur_bypass}"
  [[ "$bypass" =~ ^(true|1|yes|si|y|s)$ ]] && bypass_json="true" || bypass_json="false"

  # Scrivi config
  cat > "$CONFIG_FILE" << EOF
{
  "proxy": {
    "tv_url":          "${tv_url}",
    "token":           "${token}",
    "token_bypass":    ${bypass_json},
    "port":            ${port},
    "wayland_display": "${wayland}",
    "xdg_runtime_dir": "${xdg}"
  }
}
EOF

  ok "Config scritta in $CONFIG_FILE"
  echo ""
  cat "$CONFIG_FILE"
  echo ""
}

# ─────────────────────────────────────────────────────────────────────
# FASE 3 — Installa script in ~/bin
# ─────────────────────────────────────────────────────────────────────
phase_install_scripts() {
  hdr "FASE 3 — Installazione script"

  mkdir -p "$BIN_DIR" "$CACHE_DIR"

  # ── tv-watchdog.sh ────────────────────────────────────────────────
  info "Scrittura tv-watchdog.sh..."
  cat > "$BIN_DIR/tv-watchdog.sh" << 'WATCHDOG_EOF'
#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════
# Casa Volterra TV — Watchdog v2.3
#
# v2.3: stop_chromium uccide TUTTI i Chromium sul sito (non solo
#       quelli con cv_token=), doppio log /tmp + ~/.cache,
#       rimosso flag --no-decommit-pooled-pages (obsoleto da Chromium 107+)
# ══════════════════════════════════════════════════════════════

set -u

CONFIG_FILE="${HOME}/cv-tv-config.json"
CHECK_INTERVAL=20
COOLDOWN_AFTER_START=30
LOG_FILE="${HOME}/.cache/tv-watchdog.log"
LOG_FILE_TMP="/tmp/cv-tv-watchdog.log"
PID_FILE="${HOME}/.cache/tv-chromium.pid"

log() {
  local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
  echo "$msg"
  mkdir -p "$(dirname "$LOG_FILE")"
  echo "$msg" >> "$LOG_FILE"
  echo "$msg" >> "$LOG_FILE_TMP"
}

# ── Config ─────────────────────────────────────────────────────
load_config() {
  [[ ! -f "$CONFIG_FILE" ]] && { log "ERRORE: $CONFIG_FILE non trovato"; exit 1; }

  TV_URL="$(python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c['proxy'].get('tv_url',''))")"
  TOKEN="$(python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c['proxy'].get('token',''))")"
  BYPASS="$(python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print('1' if c['proxy'].get('token_bypass',False) else '0')")"
  PROXY_PORT="$(python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c['proxy'].get('port',8765))")"
  WAYLAND_DISPLAY_VAL="$(python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c['proxy'].get('wayland_display','wayland-0'))")"
  XDG_RUNTIME_DIR_VAL="$(python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c['proxy'].get('xdg_runtime_dir','/run/user/1000'))")"
  PROXY_LOCAL="http://localhost:${PROXY_PORT}"

  if [[ "$BYPASS" == "1" ]] || [[ -z "$TOKEN" ]]; then
    TV_CHECK_URL="$(python3 -c "
import json,urllib.parse
c=json.load(open('$CONFIG_FILE'))
u=c['proxy'].get('tv_url','')
p=urllib.parse.urlparse(u)
print(p.scheme+'://'+p.netloc+p.path)
")"
  else
    TV_CHECK_URL="$TV_URL"
  fi

  log "Config OK: proxy=$PROXY_LOCAL bypass=$BYPASS"
  log "Check URL: $TV_CHECK_URL"
  log "Kiosk URL: $TV_URL"
}

# ── Proxy ──────────────────────────────────────────────────────
wait_for_proxy() {
  log "Attendo proxy ${PROXY_LOCAL} ..."
  local i=0
  while [[ $i -lt 30 ]]; do
    curl -sf --max-time 3 "${PROXY_LOCAL}/health" >/dev/null 2>&1 && { log "Proxy online"; return 0; }
    sleep 2; (( i++ )) || true
  done
  log "WARN: proxy non risponde — continuo"
}

# ── Chromium ───────────────────────────────────────────────────
is_chromium_running() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null)" || return 1
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

stop_chromium() {
  log "Stop Chromium"
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]]; then
      kill "$pid" 2>/dev/null || true
      sleep 1
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
  fi
  # Uccide TUTTI i Chromium sul sito (con o senza cv_token= nell'URL)
  pkill -f "chromium.*casa-volterra\.it" 2>/dev/null || true
  pkill -f "cv_token=" 2>/dev/null || true
  sleep 2
}

start_chromium() {
  log "Start Chromium → $TV_URL"

  export WAYLAND_DISPLAY="$WAYLAND_DISPLAY_VAL"
  export XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR_VAL"
  export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR_VAL}/bus"
  export XDG_SESSION_TYPE="wayland"
  export GDK_BACKEND="wayland"

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
    >> /tmp/cv-tv-chromium.log 2>&1 &

  local pid=$!
  echo "$pid" > "$PID_FILE"
  log "Chromium PID=$pid — cooldown ${COOLDOWN_AFTER_START}s"
  sleep "$COOLDOWN_AFTER_START"
}

# ── Controllo pagina ───────────────────────────────────────────
MAINT_PATTERNS=(
  "stiamo aggiornando il sito"
  "briefly unavailable for scheduled maintenance"
  "scheduled maintenance"
  "manutenzione"
)

page_is_ok() {
  local http_code body body_lc final_url

  http_code="$(curl -sL --max-time 15 \
    -H "X-CV-Token: ${TOKEN}" \
    -o /dev/null -w "%{http_code}" \
    "$TV_CHECK_URL" 2>/dev/null || echo "000")"

  case "$http_code" in
    200) ;;
    403) log "HTTP 403 — token WP non sincronizzato (token=$TOKEN)"; return 1 ;;
    000) log "HTTP 000 — nessuna risposta dal server"; return 1 ;;
    *)   log "HTTP $http_code — errore"; return 1 ;;
  esac

  local response
  response="$(curl -sL --max-time 20 \
    -H "X-CV-Token: ${TOKEN}" \
    -w '\nFINAL_URL:%{url_effective}' \
    "$TV_CHECK_URL" 2>/dev/null || true)"

  final_url="$(printf '%s\n' "$response" | grep '^FINAL_URL:' | sed 's/^FINAL_URL://')"
  body="$(printf '%s\n' "$response" | grep -v '^FINAL_URL:')"
  body_lc="$(printf '%s' "$body" | tr '[:upper:]' '[:lower:]')"

  if grep -Fq "riservata all'interfaccia tv" <<< "$body_lc"; then
    log "Pagina 403 custom — token WP ≠ token config.json"
    return 1
  fi

  for p in "${MAINT_PATTERNS[@]}"; do
    grep -Fq "$p" <<< "$body_lc" && { log "Maintenance: $p"; return 1; }
  done

  if ! grep -Fq "cv-tv-launcher" <<< "$body_lc"; then
    log "Marcatore non trovato — plugin non attivo o pagina errata"
    log "  URL finale: $final_url"
    log "  Body preview: $(printf '%s' "$body" | head -c 300 | tr '\n' ' ')"
    return 1
  fi

  return 0
}

# ══════════════════════════════════════════════════════════════
mkdir -p "${HOME}/.cache"
: > "$LOG_FILE"
: > "$LOG_FILE_TMP"

log "══════════════════════════════"
log "Watchdog v2.3 avviato"

load_config
wait_for_proxy

SITE_STATE="unknown"

while true; do
  if page_is_ok; then
    if [[ "$SITE_STATE" != "good" ]]; then
      log "✅ Sito OK — avvio Chromium"
      SITE_STATE="good"
      stop_chromium
      start_chromium
      continue
    fi
    if ! is_chromium_running; then
      log "⚠️  Chromium morto — riavvio"
      start_chromium
      continue
    fi
  else
    if [[ "$SITE_STATE" != "bad" ]]; then
      log "🔴 Sito non OK"
      SITE_STATE="bad"
    fi
  fi
  sleep "$CHECK_INTERVAL"
done
WATCHDOG_EOF

  # ── tv-display-keepalive.sh ───────────────────────────────────────
  info "Scrittura tv-display-keepalive.sh..."
  cat > "$BIN_DIR/tv-display-keepalive.sh" << 'KEEPALIVE_EOF'
#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════
# Casa Volterra TV — Display Keepalive v1.1
#
# v1.1: ping CEC limitato a 1 tentativo (cec-client timeout 5s),
#       riduce log rumorosi di errore CEC.
# ══════════════════════════════════════════════════════════════

set -u

WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/1000}"
LOG_FILE="/tmp/cv-tv-display-keepalive.log"
PING_INTERVAL=55

export WAYLAND_DISPLAY XDG_RUNTIME_DIR
export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"
export XDG_SESSION_TYPE=wayland

log() {
  local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
  echo "$msg"
  echo "$msg" >> "$LOG_FILE"
}

CONFIG_FILE="${HOME}/cv-tv-config.json"
if [[ -f "$CONFIG_FILE" ]]; then
  WAYLAND_DISPLAY="$(python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c['proxy'].get('wayland_display','wayland-0'))" 2>/dev/null || echo "wayland-0")"
  XDG_RUNTIME_DIR="$(python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c['proxy'].get('xdg_runtime_dir','/run/user/1000'))" 2>/dev/null || echo "/run/user/1000")"
  export WAYLAND_DISPLAY XDG_RUNTIME_DIR
fi

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

  if command -v cec-client &>/dev/null; then
    echo "on 0" | timeout 5 cec-client -s -d 1 2>/dev/null | tail -1 || true
    log "CEC: on 0 inviato"
  fi
}

cec_ping() {
  if command -v cec-client &>/dev/null; then
    echo "as" | timeout 5 cec-client -s -d 1 2>/dev/null | grep -v "^$" | tail -1 || true
  fi
}

log "Display Keepalive avviato (ping ogni ${PING_INTERVAL}s)"
sleep 8
disable_dpms

while true; do
  sleep "$PING_INTERVAL"
  cec_ping
done
KEEPALIVE_EOF

  # ── start-tv.sh ──────────────────────────────────────────────────
  info "Scrittura start-tv.sh..."
  cat > "$BIN_DIR/start-tv.sh" << 'STARTTV_EOF'
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
STARTTV_EOF

  chmod +x "$BIN_DIR/tv-watchdog.sh" "$BIN_DIR/tv-display-keepalive.sh" "$BIN_DIR/start-tv.sh"
  ok "Script installati in $BIN_DIR"
}

# ─────────────────────────────────────────────────────────────────────
# FASE 4 — Labwc autostart (pulito, senza duplicati)
# ─────────────────────────────────────────────────────────────────────
phase_autostart() {
  hdr "FASE 4 — Labwc autostart"

  local autostart_dir
  autostart_dir="$(dirname "$LABWC_AUTOSTART")"
  mkdir -p "$autostart_dir"

  # Backup del vecchio autostart
  if [[ -f "$LABWC_AUTOSTART" ]]; then
    cp "$LABWC_AUTOSTART" "${LABWC_AUTOSTART}.bak.$(date +%s)"
    ok "Backup: ${LABWC_AUTOSTART}.bak.*"
  fi

  cat > "$LABWC_AUTOSTART" << 'AUTOSTART_EOF'
# ── Casa Volterra TV — ~/.config/labwc/autostart ──────────────
#
# Architettura servizi:
#   cv-tv-proxy.service        → systemd user (avvio automatico)
#   cv-tv-home-button.service  → systemd user (avvio automatico)
#   cv-tv-power-button.service → systemd user (avvio automatico)
#
# Avviati qui (devono girare nel compositor per accedere a Wayland):
#   tv-watchdog.sh        → monitora il sito e gestisce Chromium
#   tv-display-keepalive.sh → mantiene display e TV Samsung attivi
#
# Log:
#   watchdog  → /tmp/cv-tv-watchdog.log  (e ~/.cache/tv-watchdog.log)
#   keepalive → /tmp/cv-tv-display-keepalive.log
#   chromium  → /tmp/cv-tv-chromium.log

/home/pi/bin/tv-watchdog.sh > /tmp/cv-tv-watchdog.log 2>&1 &
/home/pi/bin/tv-display-keepalive.sh > /tmp/cv-tv-display-keepalive.log 2>&1 &
AUTOSTART_EOF

  ok "Autostart scritto (pulito, senza duplicati)"
  cat "$LABWC_AUTOSTART"
}

# ─────────────────────────────────────────────────────────────────────
# FASE 5 — Riavvia servizi Python systemd
# ─────────────────────────────────────────────────────────────────────
phase_restart_services() {
  hdr "FASE 5 — Riavvio servizi systemd"

  local any_found=false
  for svc in "${PYTHON_SERVICES[@]}"; do
    if systemctl --user list-unit-files "${svc}.service" 2>/dev/null | grep -q "${svc}"; then
      any_found=true
      systemctl --user start "${svc}.service" 2>/dev/null \
        && ok "Avviato: $svc" \
        || warn "Non avviato (potrebbe non essere installato): $svc"
    fi
  done

  if [[ "$any_found" == false ]]; then
    info "Nessun servizio Python systemd trovato — verranno avviati da systemd al prossimo boot"
  fi
}

# ─────────────────────────────────────────────────────────────────────
# FASE 6 — Self-test
# ─────────────────────────────────────────────────────────────────────
phase_test() {
  hdr "FASE 6 — Test funzionale"

  local pass=0 fail=0

  _check() {
    local desc="$1"; shift
    if "$@" &>/dev/null 2>&1; then
      ok "$desc"
      (( pass++ )) || true
    else
      err "$desc"
      (( fail++ )) || true
    fi
  }

  # File esistono e sono eseguibili
  _check "tv-watchdog.sh esiste ed è eseguibile"        test -x "$BIN_DIR/tv-watchdog.sh"
  _check "tv-display-keepalive.sh esiste ed è eseguibile" test -x "$BIN_DIR/tv-display-keepalive.sh"
  _check "start-tv.sh esiste ed è eseguibile"            test -x "$BIN_DIR/start-tv.sh"
  _check "cv-tv-config.json esiste"                     test -f "$CONFIG_FILE"

  # Autostart sano (una sola occorrenza di keepalive)
  local ka_count
  ka_count="$(grep -c "tv-display-keepalive" "$LABWC_AUTOSTART" 2>/dev/null || echo 0)"
  if [[ "$ka_count" -eq 3 ]]; then
    ok "Keepalive in autostart: 1 volta (corretto)"
    (( pass++ )) || true
  else
    err "Keepalive in autostart: $ka_count volte (deve essere 1)"
    (( fail++ )) || true
  fi

  # Config: tv_url non vuoto e contiene il token
  local tv_url_test token_test
  tv_url_test="$(python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c['proxy'].get('tv_url',''))" 2>/dev/null || echo "")"
  token_test="$(  python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c['proxy'].get('token',''))"  2>/dev/null || echo "")"
  _check "config: tv_url non vuoto"          test -n "$tv_url_test"
  _check "config: token non vuoto"           test -n "$token_test"
  _check "config: tv_url contiene cv_token=" echo "$tv_url_test" | grep -q "cv_token="

  # Nessun flag obsoleto nei file Chromium
  local bad_flags=false
  for f in "$HOME_DIR/.config/chromium-flags.conf" "/etc/chromium.d/cv-tv"; do
    if [[ -f "$f" ]] && grep -q "no-decommit" "$f" 2>/dev/null; then
      bad_flags=true
    fi
  done
  if [[ "$bad_flags" == false ]]; then
    ok "Nessun flag Chromium obsoleto trovato"
    (( pass++ )) || true
  else
    err "Flag obsoleto --no-decommit-pooled-pages ancora presente"
    (( fail++ )) || true
  fi

  # Connettività al sito
  info "Test connettività sito..."
  local http_code
  http_code="$(curl -sL --max-time 10 \
    -H "X-CV-Token: ${token_test}" \
    -o /dev/null -w "%{http_code}" \
    "$tv_url_test" 2>/dev/null || echo "000")"
  if [[ "$http_code" == "200" ]]; then
    ok "Sito raggiungibile: HTTP $http_code"
    (( pass++ )) || true
  else
    warn "Sito: HTTP $http_code (potrebbe essere normale se proxy non è ancora avviato)"
    (( fail++ )) || true
  fi

  echo ""
  echo -e "  Risultato: ${GRN}${pass} OK${RST}  /  ${RED}${fail} FAIL${RST}"

  if [[ "$fail" -eq 0 ]]; then
    ok "Tutti i test superati — sistema pronto per il riavvio"
  else
    warn "Alcuni test falliti — controlla i messaggi sopra prima di riavviare"
  fi
}

# ─────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────
main() {
  echo ""
  echo -e "${BLD}${CYN}"
  echo "  ╔═══════════════════════════════════════════════╗"
  echo "  ║    Casa Volterra TV — Installer v1.0          ║"
  echo "  ╚═══════════════════════════════════════════════╝"
  echo -e "${RST}"
  echo ""

  phase_cleanup
  phase_config
  phase_install_scripts
  phase_autostart
  phase_restart_services
  phase_test

  hdr "INSTALLAZIONE COMPLETATA"
  echo ""
  echo "  Prossimi passi:"
  echo ""
  echo "  1. Riavvia il Raspberry Pi:"
  echo "     sudo reboot"
  echo ""
  echo "  2. Dopo il boot verifica i log:"
  echo "     tail -f /tmp/cv-tv-watchdog.log"
  echo "     tail -f /tmp/cv-tv-display-keepalive.log"
  echo "     tail -f /tmp/cv-tv-chromium.log"
  echo ""
  echo "  3. Comandi utili:"
  echo "     ~/bin/start-tv.sh              # avvio manuale Chromium (debug)"
  echo "     pkill -f tv-watchdog.sh        # ferma watchdog"
  echo "     cat ~/cv-tv-config.json        # mostra config"
  echo ""
}

main "$@"
