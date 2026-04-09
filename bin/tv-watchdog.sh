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
