# Raspberry Pi Smart TV System

Sistema completo per trasformare un **Raspberry Pi 5** in una Smart TV con gestione CEC, telecomando AirMouse, Kodi, integrazione WordPress e plugin Home Assistant.

Sviluppato per installazione residenziale/hospitality su TV Philips con EasyLink (HDMI-CEC), testato su **Raspberry Pi 5** con Raspberry Pi OS Bookworm (Wayland/labwc).

---

## Caratteristiche principali

- **Proxy HTTP locale** (`cv-tv-proxy.py`) — cuore del sistema: gestisce input da AirMouse via `evdev`, controllo CEC TV (accensione/standby/switch input), volume PipeWire/ALSA, RPC Kodi, watchdog Chromium
- **Kiosk browser** — Chromium in modalità kiosk su Wayland, auto-riavvio tramite watchdog
- **Controllo CEC completo** — accensione/standby TV Philips, switch tra HDMI (Pi ↔ Firestick), inoltro comandi CEC a dispositivi downstream
- **Telecomando AirMouse** — mappatura tasti evdev configurabile via JSON, nessun driver aggiuntivo
- **OSD Volume** — barra visuale volume tramite `wob` (Wayland On-screen Bar)
- **Integrazione Kodi** — avvio, arresto e RPC JSON da telecomando
- **Plugin WordPress** — pagina TV servita dal tuo sito WordPress con autenticazione token
- **Chrome Extension** — pulsanti Home/Back sovrapposti, tastiera virtuale, scroll polling
- **Servizi systemd** — avvio automatico, restart on-failure, log su journald

---

## Architettura

```
[AirMouse USB] ──evdev──► [cv-tv-proxy.py :8765]
                                  │
                    ┌─────────────┼──────────────┐
                    ▼             ▼              ▼
              [cec-client]  [Chromium kiosk]  [Kodi RPC]
                    │             │
                    ▼             ▼
              [TV via CEC]  [Sito WordPress]
                                  │
                            [Plugin WP]
                            (autenticazione token,
                             programmazione, guide,
                             playlist, AI, webcams)
```

**Topologia CEC tipica:**
- TV = indirizzo logico 0 (root)
- Raspberry Pi = LA 8 (Playback Device 2), HDMI-1
- Amazon Firestick = LA 4 (Playback Device 1), HDMI-2

---

## Requisiti hardware

| Componente | Dettagli |
|---|---|
| Raspberry Pi 5 | 4 GB RAM consigliato (testato), 8 GB per uso intensivo |
| MicroSD | 16 GB classe 10 / A1 minimo |
| Adattatore CEC USB | es. Pulse-Eight USB-CEC (consigliato) |
| Telecomando AirMouse | qualsiasi HID USB/BT con codici evdev standard |
| TV | con supporto HDMI-CEC (es. Philips EasyLink, Samsung Anynet+, LG SimpLink) |
| Rete | Ethernet o WiFi, connessione al sito WordPress |

---

## Requisiti software

```bash
# Sistema operativo
Raspberry Pi OS Bookworm (64-bit) con desktop Wayland (labwc)
```

> **Installazione automatica:** usa lo script `install-deps.sh` incluso (vedi sotto).

---

## Installazione rapida

### 1. Clona il repository

```bash
git clone https://github.com/risik01/raspberry-smart-tv.git
cd raspberry-smart-tv
```

### 2. Installa le dipendenze

```bash
chmod +x install-deps.sh
bash install-deps.sh
```

Lo script installa automaticamente tutti i pacchetti necessari:

| Pacchetto | Versione testata | Uso |
|---|---|---|
| `python3` | 3.13+ | Runtime principale del proxy |
| `python3-evdev` | 1.9+ | Lettura input AirMouse da `/dev/input/` |
| `python3-psutil` | 6.x+ | Monitor processi (Chromium, Kodi) |
| `python3-requests` | 2.32+ | HTTP negli script bash (watchdog) |
| `chromium` | 146+ | Browser kiosk Wayland |
| `kodi` | 21.x (Omega) | Media center con JSON-RPC |
| `cec-utils` | 7.0+ | Controllo TV via HDMI-CEC (`cec-client`) |
| `wob` | 0.14+ | OSD barra volume a schermo |
| `ffmpeg` | 7.x+ | Encoding PiP / stream video |
| `labwc` | 0.9+ | Compositor Wayland (Raspberry Pi OS default) |
| `wlr-randr` | 0.4+ | Gestione output display Wayland |
| `wlopm` | 0.1+ | Controllo DPMS display Wayland |
| `pipewire` + `wireplumber` | 1.4+ | Stack audio (PipeWire) |
| `curl`, `jq`, `git` | — | Strumenti di sistema |

> Dopo l'installazione potrebbe essere necessario un **riavvio** per applicare i permessi sui gruppi `input`/`video`/`audio`.

### 3. Configura

```bash
cp cv-tv-config.example.json cv-tv-config.json
nano cv-tv-config.json
```

Modifica almeno:
- `proxy.tv_url` → URL della pagina TV sul tuo sito WordPress con il token
- `proxy.token` → il tuo token segreto (deve corrispondere a quello nel plugin WP)
- `homeassistant.url` → IP/URL della tua istanza Home Assistant (opzionale)

### 4. Installa il sistema

```bash
chmod +x cv-tv-install.sh
bash cv-tv-install.sh
```

Lo script esegue automaticamente:
1. Pulizia processi esistenti
2. Copia file nelle posizioni corrette (`~/bin/`, servizi systemd)
3. Crea/aggiorna `cv-tv-config.json`
4. Configura l'autostart di labwc
5. Abilita e avvia i servizi systemd
6. Verifica che tutto sia attivo

### 5. Installa il plugin WordPress

1. Vai in `plugin-wordpress/`
2. Carica `casa-volterra-tv-launcher.zip` nel pannello Plugin di WordPress
3. Attiva il plugin
4. Configura il token in **Impostazioni → CV TV Launcher**: deve corrispondere a `proxy.token` in `cv-tv-config.json`
5. Crea una pagina WordPress con lo shortcode `[cv_tv_launcher]`

---

## Struttura del repository

```
raspberry-smart-tv/
│
├── cv-tv-proxy.py              # Proxy principale (evdev, CEC, volume, Kodi RPC)
├── cv-tv-config.example.json   # Configurazione template (copia → cv-tv-config.json)
├── install-deps.sh             # Installazione tutte le dipendenze (apt + permessi)
├── cv-tv-install.sh            # Script di installazione sistema
├── cv-tv-proxy.service         # Servizio systemd proxy
├── cv-tv-power-button.service  # Servizio systemd pulsante power
│
├── bin/
│   ├── start-tv.sh             # Avvio manuale Chromium (debug/test)
│   ├── tv-watchdog.sh          # Watchdog: monitora sito + riavvia Chromium
│   ├── tv-display-keepalive.sh # Keepalive display: DPMS off + CEC ping periodico
│   └── cv-tv-setup-wob.sh      # Setup OSD volume (wob)
│
├── kodi/
│   ├── advancedsettings.xml            # Config avanzata Kodi (CEC standby)
│   └── peripheral_data/
│       └── cec_CEC_Adapter.xml         # Config adattatore CEC in Kodi
│
├── labwc/
│   └── autostart               # Script autostart per compositor labwc
│
├── systemd/
│   ├── cv-tv-proxy.service         # Servizio proxy
│   ├── cv-tv-home-button.service   # Servizio pulsante Home
│   └── cv-tv-power-button.service  # Servizio pulsante Power
│
├── extension/
│   ├── manifest.json           # Chrome Extension Manifest v3
│   ├── content.js              # Logica estensione (Home/Back FAB, tastiera, scroll)
│   └── content.css             # Stili pulsanti overlay
│
└── plugin-wordpress/
    └── casa-volterra-tv-launcher.zip  # Plugin WordPress (installabile da pannello WP)
```

---

## Configurazione dettagliata

### cv-tv-config.json

| Chiave | Descrizione | Default |
|---|---|---|
| `proxy.port` | Porta HTTP del proxy locale | `8765` |
| `proxy.tv_url` | URL completo della pagina TV con token | — |
| `proxy.token` | Token segreto condiviso con plugin WP | — |
| `proxy.token_bypass` | `true` = disabilita autenticazione token | `false` |
| `proxy.browser_width/height` | Risoluzione browser kiosk | `1920x1080` |
| `volume.method` | `wpctl` (PipeWire) o `amixer` (ALSA) | `wpctl` |
| `volume.step_percent` | Incremento volume per ogni pressione | `5` |
| `device.name` | Prefisso nome dispositivo evdev | `AirMouse` |
| `device.debounce_sec` | Debounce tasti in secondi | `0.25` |
| `keys` | Mappa codici evdev → azione | vedi esempio |
| `kodi.rpc_port` | Porta JSON-RPC Kodi | `8080` |

### Azioni tasti disponibili

| Azione | Effetto |
|---|---|
| `tv` | Passa a ScreenTV (HDMI-1, Chromium kiosk) |
| `firestick` | Switch a Firestick (HDMI-2) via CEC Set Stream Path |
| `kodi` | Avvia/porta in foreground Kodi |
| `back` | Tasto Back (browser o CEC Firestick) |
| `power` | Toggle accensione/standby TV via CEC |
| `vol_up` / `vol_down` | Volume +/- con OSD |
| `mute` | Mute/unmute |
| `playpause` | Play/Pausa media |
| `arrow_up/down/left/right` | Frecce (CEC Firestick se su HDMI-2, browser altrimenti) |
| `select` | Invio/click |
| `pass` | Passthrough al sistema (nessuna intercettazione) |

---

## Mappatura CEC

Il proxy gestisce CEC direttamente tramite `cec-client`. Comandi usati:

```bash
# Accensione TV
echo "on 0" | cec-client -s -d 1

# Standby TV (Philips EasyLink richiede broadcast)
echo "standby 0" | cec-client -s -d 1
echo "standby f" | cec-client -s -d 1   # broadcast

# Attiva sorgente HDMI-1 (Raspberry Pi)
echo "as" | cec-client -s -d 1

# Switch a HDMI-2 (Firestick) — raw Set Stream Path
echo "tx 8f:86:20:00" | cec-client -s -d 1

# Comandi CEC a Firestick (LA 4)
echo "tx 84:44:XX" | cec-client -s -d 1   # User Control Pressed
echo "tx 84:45" | cec-client -s -d 1      # User Control Released
```

> **Nota Philips EasyLink:** il comando `standby 0` (diretto) viene ignorato — è necessario inviare anche `standby f` (broadcast). Questo comportamento è hardcoded nel proxy.

> **Nota libCEC 7.x:** il comando `sp XXXX` non è valido, usare sempre `tx` raw per Set Stream Path.

---

## Abilitare Kodi JSON-RPC

Prima di usare le funzioni Kodi, abilita il web server:

```
Kodi → Impostazioni → Servizi → Controllo
  ✓ Consenti controllo HTTP
  Porta: 8080
  Username: kodi
  Password: kodi
```

Oppure usa lo script incluso:

```bash
bash cv-tv-kodi-enable-rpc.sh
```

---

## Servizi systemd

I servizi vengono installati come **systemd user services** (non root):

```bash
# Stato
systemctl --user status cv-tv-proxy
systemctl --user status cv-tv-home-button
systemctl --user status cv-tv-power-button

# Log
journalctl --user -u cv-tv-proxy -f

# Riavvio
systemctl --user restart cv-tv-proxy
```

---

## Chrome Extension

La directory `extension/` contiene un'estensione Chromium (Manifest v3) da caricare in modalità sviluppatore:

1. Apri `chromium://extensions`
2. Abilita **Modalità sviluppatore**
3. Clicca **Carica estensione non pacchettizzata**
4. Seleziona la cartella `extension/`

Funzionalità:
- Pulsanti FAB Home e Back sovrapposti a qualsiasi pagina
- Tastiera virtuale touch-friendly
- Scroll polling per siti che richiedono interazione continua
- Posizione pulsanti configurabile per dominio (vedi `fab_positions` in config)

---

## Plugin WordPress

Il plugin `casa-volterra-tv-launcher` per WordPress gestisce:

- **Autenticazione token** — protegge la pagina TV da accessi non autorizzati
- **Programmazione** — schedule settimanale dei contenuti
- **Guide TV** — integrazione EPG
- **Playlist** — gestione playlist video
- **Webcam** — embed webcam live
- **AI** — suggerimenti contenuti
- **REST API** — endpoint per comunicazione con il proxy

### Installazione

1. `Dashboard WordPress → Plugin → Aggiungi nuovo → Carica plugin`
2. Carica `plugin-wordpress/casa-volterra-tv-launcher.zip`
3. Attiva il plugin
4. Vai in `Impostazioni → CV TV Launcher`
5. Imposta il **Token segreto** (deve corrispondere a `proxy.token` nel file JSON)
6. Crea una pagina con lo shortcode: `[cv_tv_launcher]`

---

## Log e diagnostica

```bash
# Log proxy (systemd)
journalctl --user -u cv-tv-proxy -f

# Log watchdog
tail -f /tmp/cv-tv-watchdog.log

# Log Chromium
tail -f /tmp/cv-tv-chromium.log

# Log display keepalive
tail -f /tmp/cv-tv-display-keepalive.log

# Log OSD volume
tail -f /tmp/cv-tv-wob.log

# Stato CEC (scan dispositivi)
echo "scan" | cec-client -s -d 1

# Test volume OSD
echo 70 > /tmp/cv-tv-wob.sock
```

---

## Aggiornamento

Per aggiornare il proxy su un sistema in produzione:

```bash
# 1. Fermata sicura
systemctl --user stop cv-tv-proxy

# 2. Sostituisci cv-tv-proxy.py con la nuova versione

# 3. Riavvio
systemctl --user start cv-tv-proxy
journalctl --user -u cv-tv-proxy -f
```

> Non eseguire mai `cv-tv-install.sh` su un sistema in produzione senza aver prima verificato le modifiche — lo script ferma tutti i servizi e riapplica la configurazione da zero.

---

## Struttura CEC dei dispositivi (esempio)

```
Scan CEC bus:
device #0: TV
  address:       0.0.0.0
  active source: no
  vendor:        Philips (EasyLink)
  osd string:    TV

device #4: Playback Device 1
  address:       2.0.0.0           ← HDMI-2
  active source: no
  vendor:        Amazon (Firestick)

device #8: Playback Device 2
  address:       1.0.0.0           ← HDMI-1 (questo Raspberry Pi)
  active source: yes
  vendor:        Pulse Eight
  osd string:    Kodi
```

---

## Licenza

MIT License — vedi `LICENSE` per i dettagli.

---

## Autore

Progetto sviluppato e mantenuto da [risik01](https://github.com/risik01).
