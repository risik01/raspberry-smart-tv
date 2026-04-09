#!/usr/bin/env bash
# ╔══════════════════════════════════════════════════════════════════════╗
# ║     Raspberry Pi Smart TV — Installazione Dipendenze v1.0           ║
# ║                                                                      ║
# ║  Installa TUTTI i pacchetti necessari su Raspberry Pi OS Bookworm    ║
# ║  (testato su Raspberry Pi 5, Wayland/labwc)                         ║
# ║                                                                      ║
# ║  Esegui come utente normale (non root):                              ║
# ║    bash install-deps.sh                                              ║
# ╚══════════════════════════════════════════════════════════════════════╝

set -euo pipefail

# ── Colori ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'
BLU='\033[0;34m'; CYN='\033[0;36m'; BLD='\033[1m'; RST='\033[0m'

ok()   { echo -e "${GRN}[OK]  $*${RST}"; }
warn() { echo -e "${YLW}[!!]  $*${RST}"; }
err()  { echo -e "${RED}[ERR] $*${RST}"; }
info() { echo -e "${BLU}[>>]  $*${RST}"; }
hdr()  { echo -e "\n${BLD}${CYN}══════ $* ══════${RST}"; }
skip() { echo -e "      ${YLW}(già installato — salto)${RST}"; }

# ── Controllo OS ──────────────────────────────────────────────────────
hdr "Verifica sistema"

if [[ "$(uname -m)" != "aarch64" ]]; then
  warn "Architettura $(uname -m) — questo script è ottimizzato per aarch64 (Raspberry Pi)"
  read -rp "Continuare comunque? [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]] || { err "Annullato."; exit 1; }
fi

OS_ID=$(grep -oP '(?<=^ID=).+' /etc/os-release | tr -d '"' || echo "unknown")
OS_VER=$(grep -oP '(?<=^VERSION_CODENAME=).+' /etc/os-release | tr -d '"' || echo "unknown")
info "Sistema: $OS_ID $OS_VER ($(uname -r))"

if [[ "$OS_VER" != "bookworm" && "$OS_VER" != "bullseye" ]]; then
  warn "Versione OS non testata ($OS_VER). Consigliato: Raspberry Pi OS Bookworm."
  warn "Premi Invio per continuare, Ctrl+C per annullare."
  read -r
fi

if [[ "$(id -u)" -eq 0 ]]; then
  err "Non eseguire come root. Usa: bash install-deps.sh"
  exit 1
fi

# ── Aggiornamento repository ──────────────────────────────────────────
hdr "Aggiornamento lista pacchetti"
info "apt-get update..."
sudo apt-get update -qq
ok "Lista pacchetti aggiornata"

# ── Funzione installazione pacchetto ─────────────────────────────────
install_pkg() {
  local pkg="$1"
  if dpkg -l "$pkg" 2>/dev/null | grep -q "^ii"; then
    echo -e "  ${BLD}${pkg}${RST}"; skip
  else
    info "Installo: $pkg"
    sudo apt-get install -y "$pkg" 2>&1 | tail -3
    ok "$pkg installato"
  fi
}

# ══════════════════════════════════════════════════════════════════════
# PYTHON
# ══════════════════════════════════════════════════════════════════════
hdr "Python 3 e librerie"

install_pkg python3
install_pkg python3-pip
install_pkg python3-venv

# Librerie Python usate dal proxy e dagli script
# - evdev: lettura eventi tastiera/mouse via /dev/input/
# - psutil: monitor processi (controllo Chromium, Kodi)
# - requests: richieste HTTP negli script di diagnostica
install_pkg python3-evdev
install_pkg python3-psutil
install_pkg python3-requests

ok "Python e librerie installate"

# ══════════════════════════════════════════════════════════════════════
# KODI
# ══════════════════════════════════════════════════════════════════════
hdr "Kodi Media Center"

install_pkg kodi

# Plugin/addon consigliati per Kodi
# (opzionale — decommentare se necessari)
# install_pkg kodi-pvr-iptvsimple
# install_pkg kodi-inputstream-adaptive

ok "Kodi installato"
echo ""
warn "IMPORTANTE: dopo il primo avvio di Kodi, abilitare il JSON-RPC:"
warn "  Impostazioni → Servizi → Controllo → Consenti controllo HTTP"
warn "  Porta: 8080 | Utente: kodi | Password: kodi"

# ══════════════════════════════════════════════════════════════════════
# CHROMIUM BROWSER
# ══════════════════════════════════════════════════════════════════════
hdr "Chromium Browser (kiosk)"

install_pkg chromium

ok "Chromium installato"

# ══════════════════════════════════════════════════════════════════════
# SISTEMA WAYLAND / COMPOSITOR
# ══════════════════════════════════════════════════════════════════════
hdr "Wayland / labwc compositor"

# labwc = compositor Wayland stacking (usato da Raspberry Pi OS desktop)
install_pkg labwc
# wlr-randr = gestione output display Wayland
install_pkg wlr-randr
# wlopm = controllo alimentazione output Wayland (DPMS)
install_pkg wlopm
# xdg-utils = utilità XDG (xdg-open, ecc.)
install_pkg xdg-utils

ok "Compositor Wayland pronto"

# ══════════════════════════════════════════════════════════════════════
# AUDIO — PIPEWIRE
# ══════════════════════════════════════════════════════════════════════
hdr "Audio PipeWire"

install_pkg pipewire
install_pkg pipewire-audio
install_pkg wireplumber
# wpctl è incluso in wireplumber — usato dal proxy per il controllo volume
install_pkg pipewire-pulse

# Abilita servizi utente PipeWire (se non già attivi)
systemctl --user enable --now pipewire pipewire-pulse wireplumber 2>/dev/null \
  && ok "Servizi PipeWire abilitati" \
  || warn "Servizi PipeWire già attivi o sessione non interattiva"

ok "Audio PipeWire configurato"

# ══════════════════════════════════════════════════════════════════════
# CEC (HDMI Consumer Electronics Control)
# ══════════════════════════════════════════════════════════════════════
hdr "CEC — libcec / cec-client"

install_pkg cec-utils
# libcec include cec-client usato dal proxy per controllo TV

ok "CEC installato"
echo ""
info "Verifica adattatore CEC:"
if ls /dev/cec* 2>/dev/null; then
  ok "Dispositivo CEC trovato"
else
  warn "Nessun /dev/cec* trovato. Assicurati che:"
  warn "  - La TV sia accesa e connessa via HDMI"
  warn "  - CEC sia abilitato sulla TV (es. Philips: EasyLink ON)"
  warn "  - Il Raspberry Pi sia collegato alla porta HDMI corretta"
fi

# ══════════════════════════════════════════════════════════════════════
# WOB — On-Screen Bar (Volume OSD)
# ══════════════════════════════════════════════════════════════════════
hdr "wob — On-Screen Bar"

install_pkg wob

ok "wob installato"

# ══════════════════════════════════════════════════════════════════════
# FFMPEG (PiP / encoding video)
# ══════════════════════════════════════════════════════════════════════
hdr "FFmpeg"

install_pkg ffmpeg

ok "FFmpeg installato"

# ══════════════════════════════════════════════════════════════════════
# STRUMENTI DI SISTEMA
# ══════════════════════════════════════════════════════════════════════
hdr "Strumenti di sistema"

install_pkg curl
install_pkg wget
install_pkg jq
install_pkg git
install_pkg unzip
install_pkg evtest        # debug input evdev
install_pkg v4l-utils     # debug video/webcam (opzionale)

ok "Strumenti installati"

# ══════════════════════════════════════════════════════════════════════
# PERMESSI UTENTE
# ══════════════════════════════════════════════════════════════════════
hdr "Permessi utente"

CURRENT_USER="$(whoami)"

# input: lettura eventi /dev/input/ (AirMouse, tastiera)
if ! groups "$CURRENT_USER" | grep -qw input; then
  sudo usermod -aG input "$CURRENT_USER"
  ok "Aggiunto al gruppo: input"
else
  skip; echo -e "  ${BLD}gruppo input${RST}"; skip
fi

# video: accesso /dev/video*, /dev/cec*
if ! groups "$CURRENT_USER" | grep -qw video; then
  sudo usermod -aG video "$CURRENT_USER"
  ok "Aggiunto al gruppo: video"
else
  echo -e "  ${BLD}gruppo video${RST}"; skip
fi

# audio: accesso dispositivi audio (legacy ALSA)
if ! groups "$CURRENT_USER" | grep -qw audio; then
  sudo usermod -aG audio "$CURRENT_USER"
  ok "Aggiunto al gruppo: audio"
else
  echo -e "  ${BLD}gruppo audio${RST}"; skip
fi

ok "Permessi configurati"

# ══════════════════════════════════════════════════════════════════════
# SYSTEMD LINGERING (avvio automatico servizi user senza login attivo)
# ══════════════════════════════════════════════════════════════════════
hdr "Systemd lingering"

if loginctl show-user "$CURRENT_USER" 2>/dev/null | grep -q "Linger=yes"; then
  echo -e "  ${BLD}lingering${RST}"; skip
else
  sudo loginctl enable-linger "$CURRENT_USER"
  ok "Lingering abilitato per $CURRENT_USER"
fi

# ══════════════════════════════════════════════════════════════════════
# RIEPILOGO
# ══════════════════════════════════════════════════════════════════════
hdr "Riepilogo installazione"

echo ""
echo -e "${BLD}Versioni installate:${RST}"
python3 --version
chromium --version 2>/dev/null | head -1 || echo "Chromium: OK"
kodi --version 2>/dev/null | head -1 || dpkg -l kodi 2>/dev/null | grep "^ii" | awk '{print "Kodi", $3}'
cec-client --version 2>/dev/null | head -1 || echo "cec-client: OK"
ffmpeg -version 2>/dev/null | head -1 || echo "FFmpeg: OK"
echo ""

ok "Tutte le dipendenze installate!"
echo ""
warn "NOTA: alcune modifiche ai gruppi richiedono il riavvio della sessione."
warn "      Se il proxy non legge /dev/input/*, esegui: sudo reboot"
echo ""
info "Passo successivo:"
echo "    cp cv-tv-config.example.json cv-tv-config.json"
echo "    nano cv-tv-config.json   # inserisci il tuo token e URL WordPress"
echo "    bash cv-tv-install.sh"
echo ""
