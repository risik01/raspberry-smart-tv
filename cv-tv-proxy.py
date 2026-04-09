#!/usr/bin/env python3
"""
Casa Volterra TV — Proxy v8.5
Legge configurazione da /home/pi/cv-tv-config.json

Novità v8.3:
  - CEC: sessione cec-client -t p PERSISTENTE invece di one-shot (-s)
    Elimina il bug in cui -s mandava automaticamente 'on 0' all'init
    prima ancora di eseguire standby — causando TV che non si spegneva.
  - Raspberry dichiarato active source (as) all'avvio: TV si accende
    automaticamente sull'input HDMI corretto.
  - Rimosso fast-path _tv_is_standby / CEC_STANDBY_GRACE artificiale.
  - _power_toggle: solo query pow 0 + azione diretta, senza grace period.

Novità v8.2:
  - FIX #2: Device watchdog per AirMouse
  - FIX #3: Rotazione automatica log
"""

import collections
import http.server
import json
import os
import select
import struct
import re
import shlex
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# ── Sessione CEC persistente ──────────────────────────────────
# Usiamo cec-client -t p in modalità interattiva invece di -s per ogni
# comando. Con -s cec-client mandava 'on 0' + active source automaticamente
# all'init, interferendo con standby. Con la sessione persistente i comandi
# vengono eseguiti senza re-init del bus.

_cec_proc          = None
_cec_session_lock  = threading.Lock()
_cec_out_lines     = collections.deque(maxlen=100)   # (timestamp, line_lower)
_cec_out_event     = threading.Event()


def _cec_reader(proc):
    """Thread: legge stdout di cec-client persistente e lo bufferizza."""
    try:
        for line in iter(proc.stdout.readline, ''):
            if not line:
                break
            stripped = line.rstrip()
            _log(f'[cec] {stripped}')
            _cec_out_lines.append((time.time(), stripped.lower()))
            _cec_out_event.set()
    except Exception as e:
        _log(f'[cec-reader] errore: {e}')


def _cec_start():
    """Avvia (o riavvia) la sessione cec-client persistente come playback device."""
    global _cec_proc
    with _cec_session_lock:
        if _cec_proc and _cec_proc.poll() is None:
            return  # già attivo
        _log('[cec] avvio sessione persistente cec-client -t p -d 1')
        try:
            _cec_proc = subprocess.Popen(
                ['cec-client', '-t', 'p', '-d', '1'],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=_wenv()
            )
            threading.Thread(target=_cec_reader, args=(_cec_proc,), daemon=True).start()
            _log(f'[cec] processo avviato PID={_cec_proc.pid}')
        except Exception as e:
            _log(f'[cec] errore avvio: {e}')
            _cec_proc = None


def _cec_send(cmd: str) -> bool:
    """Invia un comando alla sessione CEC persistente."""
    _cec_start()
    proc = _cec_proc
    if not proc or proc.poll() is not None:
        _log(f'[cec] processo non disponibile, comando ignorato: {cmd}')
        return False
    try:
        proc.stdin.write(cmd + '\n')
        proc.stdin.flush()
        _log(f'[cec] → {cmd}')
        return True
    except Exception as e:
        _log(f'[cec] errore invio "{cmd}": {e}')
        return False


def _cec_query_power(timeout: float = 5.0) -> str:
    """Invia 'pow 0' e aspetta la risposta. Ritorna: 'on', 'standby', 'unknown', ecc."""
    ts_before = time.time()
    _cec_out_event.clear()
    _cec_send('pow 0')
    deadline = time.time() + timeout
    while time.time() < deadline:
        _cec_out_event.wait(timeout=0.3)
        _cec_out_event.clear()
        for ts, line in reversed(list(_cec_out_lines)):
            if ts < ts_before:
                break
            m = re.search(r'power status:\s*(.+)', line)
            if m:
                status = m.group(1).strip()
                _log(f'[cec] stato TV parsed: "{status}"')
                return status
    _log('[cec] query power: timeout — stato sconosciuto')
    return 'unknown'


def _cec_force_restart():
    """Termina e riavvia la sessione CEC, aspettando la reinizializzazione.
    Usato quando la sessione era partita con TV spenta e l'indirizzo logico
    sul bus CEC non è stato negoziato correttamente."""
    global _cec_proc
    _log('[cec] force restart sessione (bus CEC non risponde)...')
    with _cec_session_lock:
        if _cec_proc and _cec_proc.poll() is None:
            try:
                _cec_proc.terminate()
                _cec_proc.wait(timeout=3)
            except Exception:
                try:
                    _cec_proc.kill()
                except Exception:
                    pass
            _cec_proc = None
    _cec_start()
    # Aspetta che cec-client rinegozioni il bus ("waiting for input")
    deadline = time.time() + 12
    while time.time() < deadline:
        _cec_out_event.wait(timeout=0.5)
        _cec_out_event.clear()
        if any('waiting for input' in ln for _, ln in list(_cec_out_lines)):
            break
    _log('[cec] force restart completato')


def _cec_watchdog():
    """Thread watchdog: riavvia la sessione CEC se il processo muore."""
    while True:
        time.sleep(30)
        if _cec_proc and _cec_proc.poll() is not None:
            _log('[cec-watchdog] processo terminato, riavvio...')
            _cec_start()


CONFIG_PATH = os.environ.get('CV_TV_CONFIG', '/home/pi/cv-tv-config.json')
LOG_PATH     = '/tmp/cv-tv-keyboard.log'
KEY_LOG_PATH = '/tmp/cv-tv-keylog.txt'
KODI_LOG_PATH = '/tmp/cv-tv-kodi.log'

# ── FIX #3: Rotazione log ─────────────────────────────────────
LOG_MAX_BYTES = 2 * 1024 * 1024  # 2 MB


def _rotate_log_if_needed():
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            rotated = LOG_PATH + '.1'
            if os.path.exists(rotated):
                os.remove(rotated)
            os.rename(LOG_PATH, rotated)
    except Exception:
        pass


def load_config():
    with open(CONFIG_PATH, encoding='utf-8') as f:
        return json.load(f)


# ── Configurazione servizi streaming ──────────────────────────
NETFLIX_PROFILE = '/home/pi/.chromium-netflix'
NETFLIX_URL = 'https://www.netflix.com/browse'
NETFLIX_LOGIN_URL = 'https://www.netflix.com/login'
_netflix_proc = None

PRIME_PROFILE = '/home/pi/.chromium-prime'
PRIME_URL = 'https://www.primevideo.com/'
PRIME_LOGIN_URL = 'https://www.primevideo.com/'
_prime_proc = None

DISNEY_PROFILE = '/home/pi/.chromium-disney'
DISNEY_URL = 'https://www.disneyplus.com/'
DISNEY_LOGIN_URL = 'https://www.disneyplus.com/login'
_disney_proc = None

HA_PROFILE = '/home/pi/.chromium-ha'
_ha_proc = None

CFG = load_config()
PORT = int(CFG['proxy']['port'])
TV_URL = CFG['proxy']['tv_url']
KODI_CMD = CFG['proxy']['kodi_cmd']
WAYLAND_DISPLAY = CFG['proxy']['wayland_display']
XDG_RUNTIME_DIR = CFG['proxy']['xdg_runtime_dir']

# ── Configurazione browser ────────────────────────────────────
BROWSER_PROFILE = '/home/pi/.chromium-web'
EXT_DIR = '/home/pi/cv-tv-extension'
_browser_proc = None
_ha_cfg = CFG.get('homeassistant', {})
HA_URL = _ha_cfg.get('url', 'http://127.0.0.1:8123')
HA_PROFILE = _ha_cfg.get('profile', HA_PROFILE)
_FAB_POSITIONS = CFG.get('fab_positions', {})
BROWSER_WIDTH  = int(CFG['proxy'].get('browser_width', 1920))
INPUT_DEVICE   = CFG['proxy'].get('input_device', '')
BROWSER_HEIGHT = int(CFG['proxy'].get('browser_height', 1080))

# ── Webcam proxy ──────────────────────────────────────────────
ALLOWED = (
    'hd-auth.skylinewebcams.com',
    'livee.skylinewebcams.com',
    'cdn.skylinewebcams.com',
    'skylinewebcams.com',
    'images-webcams.windy.com',
    'webcams.windy.com',
    'rtsp.me',
)

FAKE_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    ),
    'Referer': 'https://www.skylinewebcams.com/',
    'Origin': 'https://www.skylinewebcams.com',
    'Accept': '*/*',
}

PROXY_BASE = f'http://localhost:{PORT}/stream?url='
TOKEN_TTL = 240

_token_cache = {}
_lock = threading.Lock()
_cec_lock = threading.Lock()
_current_app = 'tv'
_current_hdmi = 'hdmi1'   # 'hdmi1' = Pi/ScreenTV, 'hdmi2' = Firestick
_pending_command = None
_pending_scroll   = None
_pending_playpause = False
_kodi_proc = None
_keyboard_proc = None
_user_tv_off = False

# ── FIX #2: watchdog device AirMouse ─────────────────────────
# Set dei device path attualmente monitorati da un thread listener.
_active_listener_devices = set()
_active_listener_lock = threading.Lock()


# ── Logging ───────────────────────────────────────────────────

def _log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        _rotate_log_if_needed()
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def _runlog(cmd, env=None, timeout=8):
    try:
        r = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        _log(f"[cmd] {' '.join(cmd)} rc={r.returncode}")
        if r.stdout and r.stdout.strip():
            _log(f"[cmd][stdout] {r.stdout.strip()}")
        if r.stderr and r.stderr.strip():
            _log(f"[cmd][stderr] {r.stderr.strip()}")
        return r
    except Exception as e:
        _log(f"[cmd][EXC] {' '.join(cmd)} -> {e}")
        return None


def _pipe_to_log(pipe, prefix):
    try:
        for line in iter(pipe.readline, ''):
            if not line:
                break
            _log(f"{prefix} {line.rstrip()}")
    except Exception as e:
        _log(f"{prefix} pipe error: {e}")


# ── Wayland helpers ───────────────────────────────────────────

def _wenv():
    e = dict(os.environ)
    e['WAYLAND_DISPLAY'] = WAYLAND_DISPLAY
    e['XDG_RUNTIME_DIR'] = XDG_RUNTIME_DIR
    e['DBUS_SESSION_BUS_ADDRESS'] = f'unix:path={XDG_RUNTIME_DIR}/bus'
    e['XDG_SESSION_TYPE'] = 'wayland'
    e['GDK_BACKEND'] = 'wayland'
    return e


def _wlrctl(*args):
    try:
        r = subprocess.run(
            ['wlrctl'] + list(args),
            env=_wenv(),
            capture_output=True,
            text=True,
            timeout=5
        )
        return r.returncode == 0, r.stdout.strip()
    except FileNotFoundError:
        _log('[proxy] WARN: wlrctl non trovato')
        return False, ''
    except Exception as e:
        _log(f'[proxy] WARN: errore wlrctl: {e}')
        return False, ''


def _toplevels():
    ok, out = _wlrctl('toplevel', 'list')
    return out.lower() if ok else ''


def _focus(app_id):
    ok, _ = _wlrctl('toplevel', 'focus', f'app_id:{app_id}')
    _log(f'[launcher] focus {app_id} -> {"OK" if ok else "FAIL"}')
    return ok


def _kodi_cmd_tokens():
    try:
        if isinstance(KODI_CMD, str):
            return shlex.split(KODI_CMD)
    except Exception as e:
        _log(f'[kodi] parse command error: {e}')
    return [str(KODI_CMD)]


def _kodi_exec_names():
    names = {'kodi', 'kodi.bin', 'kodi-gbm', 'kodi-wayland'}
    try:
        toks = _kodi_cmd_tokens()
        if toks:
            names.add(os.path.basename(toks[0]).lower())
    except Exception:
        pass
    return {n for n in names if n}


def _find_kodi_processes():
    out = []
    names = _kodi_exec_names()
    try:
        r = subprocess.run(
            ['ps', '-eo', 'pid=,comm=,args='],
            capture_output=True,
            text=True,
            timeout=5
        )
        if r.returncode != 0:
            return out
        for raw in r.stdout.splitlines():
            line = raw.strip()
            if not line:
                continue
            parts = line.split(None, 2)
            if len(parts) < 2:
                continue
            pid_s, comm = parts[0], parts[1]
            args = parts[2] if len(parts) > 2 else ''
            try:
                pid = int(pid_s)
            except ValueError:
                continue
            comm_l = comm.lower()
            first_arg = os.path.basename(args.split()[0]).lower() if args.split() else ''
            if comm_l in names or first_arg in names:
                out.append({'pid': pid, 'comm': comm, 'args': args})
    except Exception as e:
        _log(f'[kodi] find process error: {e}')
    return out


def _kodi_rpc_available(timeout=1.0):
    try:
        with socket.create_connection(('127.0.0.1', 8080), timeout=timeout):
            return True
    except Exception:
        return False


def _kodi_rpc_request(payload_dict):
    import base64 as _b64
    kodi_cfg = CFG.get('kodi', {})
    user = kodi_cfg.get('rpc_user', 'kodi')
    pwd  = kodi_cfg.get('rpc_password', 'kodi')
    creds = _b64.b64encode(f'{user}:{pwd}'.encode()).decode()
    payload = json.dumps(payload_dict).encode()
    req = urllib.request.Request(
        'http://localhost:8080/jsonrpc',
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Basic {creds}',
        }
    )
    return urllib.request.urlopen(req, timeout=2)


def _launch_kodi_process():
    global _kodi_proc
    cmd = _kodi_cmd_tokens()
    _log(f'[launcher] avvio Kodi cmd={cmd}')
    try:
        logf = open(KODI_LOG_PATH, 'a', encoding='utf-8')
    except Exception:
        logf = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(
            cmd,
            env=_wenv(),
            stdout=logf,
            stderr=logf
        )
        with _lock:
            _kodi_proc = proc
        _log(f'[launcher] Kodi spawn PID={proc.pid}')
        return proc
    except FileNotFoundError as e:
        _log(f'[launcher] ERRORE Kodi command non trovato: {e}')
    except Exception as e:
        _log(f'[launcher] ERRORE avvio Kodi: {e}')
    with _lock:
        _kodi_proc = None
    return None


# ── Tastiera virtuale ─────────────────────────────────────────

def keyboard_state():
    with _lock:
        proc = _keyboard_proc
    if not proc:
        return {'pid': None, 'alive': False, 'returncode': None}
    rc = proc.poll()
    return {'pid': proc.pid, 'alive': rc is None, 'returncode': rc}


def show_keyboard():
    global _keyboard_proc

    env = _wenv()
    wayland_sock = os.path.join(env['XDG_RUNTIME_DIR'], env['WAYLAND_DISPLAY'])
    bus_sock = os.path.join(env['XDG_RUNTIME_DIR'], 'bus')

    _log('[keyboard] ===== SHOW REQUEST =====')
    _log(f"[keyboard] uid={os.getuid()} euid={os.geteuid()} user={env.get('USER')}")
    _log(f"[keyboard] WAYLAND_DISPLAY={env.get('WAYLAND_DISPLAY')}")
    _log(f"[keyboard] XDG_RUNTIME_DIR={env.get('XDG_RUNTIME_DIR')}")
    _log(f"[keyboard] DBUS_SESSION_BUS_ADDRESS={env.get('DBUS_SESSION_BUS_ADDRESS')}")
    _log(f"[keyboard] wayland_sock_exists={os.path.exists(wayland_sock)} path={wayland_sock}")
    _log(f"[keyboard] bus_sock_exists={os.path.exists(bus_sock)} path={bus_sock}")

    _runlog(['id'], env=env)
    _runlog(['which', 'squeekboard'], env=env)
    _runlog(['pgrep', '-a', '-x', 'squeekboard'], env=env)
    _runlog(
        ['gsettings', 'get', 'org.gnome.desktop.a11y.applications', 'screen-keyboard-enabled'],
        env=env
    )

    with _lock:
        proc = _keyboard_proc
        if proc and proc.poll() is None:
            _log(f'[keyboard] già aperta pid={proc.pid}')
            return

    subprocess.run(['pkill', '-x', 'squeekboard'], capture_output=True)

    try:
        proc = subprocess.Popen(
            ['squeekboard'],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )
        with _lock:
            _keyboard_proc = proc

        _log(f'[keyboard] squeekboard spawned pid={proc.pid}')

        if proc.stdout:
            threading.Thread(
                target=_pipe_to_log,
                args=(proc.stdout, '[keyboard][squeekboard]'),
                daemon=True
            ).start()

    except Exception as e:
        _log(f'[keyboard] errore avvio: {e}')
        return

    def _inspect():
        time.sleep(2)
        st = keyboard_state()
        _log(f'[keyboard] state_after_2s={st}')
        _runlog(['pgrep', '-a', '-x', 'squeekboard'], env=env)
        _runlog(
            [
                'busctl', '--user', 'call',
                'sm.puri.OSK0', '/sm/puri/OSK0',
                'sm.puri.OSK0', 'SetVisible', 'b', 'true'
            ],
            env=env
        )
        _runlog(['pgrep', '-a', '-x', 'squeekboard'], env=env)
        try:
            ok, tl = _wlrctl('toplevel', 'list')
            _log(f'[keyboard] wlrctl_ok={ok} toplevels={tl[:500] if tl else "(nessuno)"}')
        except Exception as e:
            _log(f'[keyboard] errore wlrctl inspect: {e}')

    threading.Thread(target=_inspect, daemon=True).start()


def hide_keyboard():
    global _keyboard_proc

    _log('[keyboard] ===== HIDE REQUEST =====')
    subprocess.run(['pkill', '-x', 'squeekboard'], capture_output=True)

    with _lock:
        proc = _keyboard_proc
        _keyboard_proc = None

    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except Exception as e:
            _log(f'[keyboard] terminate error: {e}')

    _runlog(['pgrep', '-a', '-x', 'squeekboard'], env=_wenv())
    _log('[keyboard] squeekboard chiusa')


# ── Browser / Streaming ───────────────────────────────────────

def _close_profile_browser(profile_path, proc_ref_name, label):
    global _netflix_proc, _prime_proc, _disney_proc, _browser_proc

    _log(f'[{label}] chiusura {label}')
    subprocess.run(['pkill', '-f', profile_path], capture_output=True)

    with _lock:
        proc = globals().get(proc_ref_name)
        globals()[proc_ref_name] = None

    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except Exception as e:
            _log(f'[{label}] terminate error: {e}')

    hide_keyboard()


def close_browser():
    global _browser_proc
    _log('[browser] chiusura browser web')
    subprocess.run(['pkill', '-f', BROWSER_PROFILE], capture_output=True)

    with _lock:
        proc = _browser_proc
        _browser_proc = None

    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except Exception as e:
            _log(f'[browser] terminate error: {e}')

    hide_keyboard()


def close_homeassistant():
    global _ha_proc
    _log('[homeassistant] chiusura Home Assistant browser')
    subprocess.run(['pkill', '-f', HA_PROFILE], capture_output=True)

    with _lock:
        proc = _ha_proc
        _ha_proc = None

    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except Exception as e:
            _log(f'[homeassistant] terminate error: {e}')

    hide_keyboard()


def close_netflix():
    _close_profile_browser(NETFLIX_PROFILE, '_netflix_proc', 'netflix')


def close_prime_video():
    _close_profile_browser(PRIME_PROFILE, '_prime_proc', 'prime')


def close_disney_plus():
    _close_profile_browser(DISNEY_PROFILE, '_disney_proc', 'disney')


def launch_browser(url='https://www.google.com'):
    global _current_app, _browser_proc

    _stop_kodi_hard()
    close_homeassistant()
    close_netflix()
    close_prime_video()
    close_disney_plus()

    _log(f'[launcher] Apro browser: {url}')

    with _lock:
        _current_app = 'browser'

    env = _wenv()
    os.makedirs(BROWSER_PROFILE, exist_ok=True)
    subprocess.run(['pkill', '-f', BROWSER_PROFILE], capture_output=True)

    cmd = [
        'chromium',
        f'--user-data-dir={BROWSER_PROFILE}',
        f'--disable-extensions-except={EXT_DIR}',
        f'--load-extension={EXT_DIR}',
        '--kiosk',
        '--start-maximized',
        '--noerrdialogs',
        '--disable-infobars',
        '--no-first-run',
        '--disable-session-crashed-bubble',
        '--autoplay-policy=no-user-gesture-required',
        '--disable-features=PrivateNetworkAccessChecks,BlockInsecurePrivateNetworkRequests',
        '--allow-running-insecure-content',
        url,
    ]

    _log(f'[browser] cmd={" ".join(cmd)}')

    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    with _lock:
        _browser_proc = proc

    _log(f'[browser] Chromium web PID={proc.pid}')


def _launch_streaming_service(profile_path, url, login_url, proc_ref_name, app_name, setup=False):
    global _current_app, _netflix_proc, _prime_proc, _disney_proc

    _stop_kodi_hard()
    close_browser()
    close_homeassistant()
    close_netflix()
    close_prime_video()
    close_disney_plus()
    hide_keyboard()

    os.makedirs(profile_path, exist_ok=True)
    try:
        os.chmod(profile_path, 0o700)
    except Exception as e:
        _log(f'[{app_name}] chmod error: {e}')

    with _lock:
        _current_app = app_name

    env = _wenv()
    target_url = login_url if setup else url

    cmd = [
        'chromium',
        f'--user-data-dir={profile_path}',
        '--noerrdialogs',
        '--disable-infobars',
        '--no-first-run',
        '--disable-session-crashed-bubble',
        '--no-default-browser-check',
        '--autoplay-policy=no-user-gesture-required',
        f'--disable-extensions-except={EXT_DIR}',
        f'--load-extension={EXT_DIR}',
    ]

    if setup:
        cmd += [
            f'--window-size={BROWSER_WIDTH},{BROWSER_HEIGHT}',
            '--window-position=0,0',
            f'--app={target_url}',
        ]
    else:
        cmd += [
            '--kiosk',
            '--start-maximized',
            target_url,
        ]

    _log(f'[{app_name}] cmd={" ".join(cmd)}')

    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    with _lock:
        globals()[proc_ref_name] = proc

    _log(f'[{app_name}] Chromium {app_name} PID={proc.pid}')


def launch_netflix(setup=False):
    _launch_streaming_service(
        profile_path=NETFLIX_PROFILE,
        url=NETFLIX_URL,
        login_url=NETFLIX_LOGIN_URL,
        proc_ref_name='_netflix_proc',
        app_name='netflix',
        setup=setup
    )


def launch_prime_video(setup=False):
    _launch_streaming_service(
        profile_path=PRIME_PROFILE,
        url=PRIME_URL,
        login_url=PRIME_LOGIN_URL,
        proc_ref_name='_prime_proc',
        app_name='prime',
        setup=setup
    )


def launch_disney_plus(setup=False):
    _launch_streaming_service(
        profile_path=DISNEY_PROFILE,
        url=DISNEY_URL,
        login_url=DISNEY_LOGIN_URL,
        proc_ref_name='_disney_proc',
        app_name='disney',
        setup=setup
    )


def launch_homeassistant():
    global _current_app, _ha_proc

    _stop_kodi_hard()
    close_browser()
    close_netflix()
    close_prime_video()
    close_disney_plus()
    hide_keyboard()

    with _lock:
        _current_app = 'homeassistant'

    env = _wenv()
    os.makedirs(HA_PROFILE, exist_ok=True)
    subprocess.run(['pkill', '-f', HA_PROFILE], capture_output=True)

    cmd = [
        'chromium',
        f'--user-data-dir={HA_PROFILE}',
        f'--disable-extensions-except={EXT_DIR}',
        f'--load-extension={EXT_DIR}',
        '--kiosk',
        '--start-maximized',
        '--noerrdialogs',
        '--disable-infobars',
        '--no-first-run',
        '--disable-session-crashed-bubble',
        '--autoplay-policy=no-user-gesture-required',
        '--disable-features=PrivateNetworkAccessChecks,BlockInsecurePrivateNetworkRequests',
        '--allow-running-insecure-content',
        HA_URL,
    ]

    _log(f'[homeassistant] cmd={" ".join(cmd)}')

    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    with _lock:
        _ha_proc = proc

    _log(f'[homeassistant] Chromium HA PID={proc.pid}')


# ── Kodi ──────────────────────────────────────────────────────

def _raise_kodi():
    ok, tl = _wlrctl('toplevel', 'list')
    _log(f"[launcher] Kodi toplevels: {tl[:500] if ok and tl else '(nessuno)'}")

    for aid in ('Kodi', 'kodi', 'kodi.bin', 'kodi-gbm', 'org.xbmc.kodi', 'xbmc'):
        if _focus(aid):
            return True

    if ok and tl:
        for line in tl.splitlines():
            parts = line.split(':', 1)
            app_id = parts[0].strip()
            title = parts[1].strip() if len(parts) > 1 else ''
            hay = f'{app_id} {title}'.lower()
            if 'kodi' not in hay and 'xbmc' not in hay:
                continue

            if app_id:
                ok2, _ = _wlrctl('toplevel', 'focus', f'app_id:{app_id}')
                _log(f'[launcher] focus app_id:{app_id!r} (da lista) -> {"OK" if ok2 else "FAIL"}')
                if ok2:
                    return True

            if title:
                ok3, _ = _wlrctl('toplevel', 'focus', f'title:{title}')
                _log(f'[launcher] focus title:{title!r} (da lista) -> {"OK" if ok3 else "FAIL"}')
                if ok3:
                    return True

    for title in ('Kodi', 'Kodi from Debian'):
        ok4, _ = _wlrctl('toplevel', 'focus', f'title:{title}')
        _log(f'[launcher] focus title:{title} -> {"OK" if ok4 else "FAIL"}')
        if ok4:
            return True

    try:
        r = subprocess.run(['wmctrl', '-a', 'Kodi'], env=_wenv(), capture_output=True, text=True)
        if r.returncode == 0:
            _log('[launcher] wmctrl Kodi -> OK')
            return True
    except FileNotFoundError:
        pass

    try:
        r2 = subprocess.run(
            ['xdotool', 'search', '--name', 'Kodi', 'windowactivate'],
            env={**_wenv(), 'DISPLAY': ':0'},
            capture_output=True,
            text=True
        )
        if r2.returncode == 0:
            _log('[launcher] xdotool Kodi -> OK')
            return True
    except FileNotFoundError:
        pass

    _log('[launcher] Nessun metodo ha trovato Kodi')
    return False


def _kodi_running():
    with _lock:
        proc = _kodi_proc

    if proc and proc.poll() is None:
        return True

    procs = _find_kodi_processes()
    if procs:
        _log('[kodi] processi reali rilevati: ' + '; '.join(
            f"pid={p['pid']} comm={p['comm']}" for p in procs[:5]
        ))
        return True

    return False


def _stop_kodi_hard():
    global _kodi_proc
    _log('[kodi] chiusura forzata Kodi')
    for name in sorted(_kodi_exec_names()):
        subprocess.run(['pkill', '-x', name], capture_output=True)
    with _lock:
        _kodi_proc = None


def _watch_kodi():
    global _current_app, _kodi_proc

    _log('[watcher] Sorveglio Kodi (via PID)...')

    confirmed = False
    for _ in range(30):
        time.sleep(0.5)
        if _kodi_running():
            _log('[watcher] Kodi confermato in esecuzione')
            confirmed = True
            break

    if not confirmed:
        _log('[watcher] Kodi non confermato entro timeout -> ritorno a TV')
        with _lock:
            if _current_app == 'kodi':
                _current_app = 'tv'
            _kodi_proc = None
        _focus_tv()
        return

    while True:
        time.sleep(2)
        with _lock:
            if _current_app != 'kodi':
                return

        if not _kodi_running():
            _log('[watcher] Kodi chiuso -> TV')
            with _lock:
                _current_app = 'tv'
                _kodi_proc = None
            time.sleep(1)
            _focus_tv()
            return


def launch_kodi():
    global _current_app, _kodi_proc

    close_browser()
    close_homeassistant()
    hide_keyboard()
    close_netflix()
    close_prime_video()
    close_disney_plus()

    with _lock:
        _current_app = 'kodi'

    should_watch = False

    if _kodi_running():
        _log('[launcher] Kodi già in esecuzione -> provo focus')
        if _raise_kodi():
            if _kodi_rpc_available():
                _kodi_resume()
            else:
                _log('[kodi] JSON-RPC non disponibile su 127.0.0.1:8080')
            should_watch = True
        else:
            _log('[launcher] Kodi rilevato ma non focalizzabile -> riavvio')
            _stop_kodi_hard()
            time.sleep(1)
            proc = _launch_kodi_process()
            if proc:
                should_watch = True
            else:
                with _lock:
                    _current_app = 'tv'
                _focus_tv()
                return
    else:
        proc = _launch_kodi_process()
        if proc:
            should_watch = True
        else:
            with _lock:
                _current_app = 'tv'
            _focus_tv()
            return

    if should_watch:
        def _wait_focus():
            for _ in range(30):
                time.sleep(0.5)
                if _kodi_running():
                    time.sleep(2)
                    if _raise_kodi():
                        _log('[launcher] Kodi -> foreground OK')
                    else:
                        _log('[launcher] Kodi avviato ma non focalizzabile')
                    return
            _log('[launcher] Kodi avvio timeout')

        threading.Thread(target=_wait_focus, daemon=True).start()
        threading.Thread(target=_watch_kodi, daemon=True).start()


# ── TV ────────────────────────────────────────────────────────

def _focus_tv():
    tl = _toplevels()
    _log(f'[launcher] toplevels: {tl[:200] or "(nessuno)"}')

    for app_id in (
        'chrome-www.casa-volterra.it__televisione_-Default',
        'chrome-www.casa-volterra.it',
        'chrome',
        'chromium',
        'chromium-browser',
    ):
        if app_id.lower() in tl.lower():
            if _focus(app_id):
                return

    for name in ('Chromium', 'chromium'):
        r = subprocess.run(
            ['wmctrl', '-a', name],
            env=_wenv(),
            capture_output=True,
            text=True
        )
        if r.returncode == 0:
            _log(f'[launcher] wmctrl {name} -> OK')
            return

    r = subprocess.run(
        ['xdotool', 'search', '--name', 'Chromium', 'windowactivate'],
        env={**_wenv(), 'DISPLAY': ':0'},
        capture_output=True,
        text=True
    )
    if r.returncode == 0:
        _log('[launcher] xdotool Chromium -> OK')
        return

    hide_keyboard()
    _log('[launcher] Chromium non trovato -> riavvio kiosk')
    subprocess.Popen(
        [
            'chromium',
            '--kiosk',
            '--noerrdialogs',
            '--disable-infobars',
            '--no-first-run',
            '--disable-session-crashed-bubble',
            '--autoplay-policy=no-user-gesture-required',
            '--disable-features=PrivateNetworkAccessChecks,BlockInsecurePrivateNetworkRequests',
            '--allow-running-insecure-content',
            '--check-for-update-interval=31536000',
            f'--app={TV_URL}',
        ],
        env=_wenv(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )


def _kodi_pause():
    try:
        _kodi_rpc_request({'jsonrpc':'2.0','method':'Player.PlayPause','params':{'playerid':1,'play':False},'id':1})
        _log('[kodi] pausa via JSON-RPC')
    except Exception as e:
        _log(f'[kodi] pausa fallita: {e}')


def _kodi_resume():
    try:
        _kodi_rpc_request({'jsonrpc':'2.0','method':'Player.PlayPause','params':{'playerid':1,'play':True},'id':1})
        _log('[kodi] ripresa via JSON-RPC')
    except Exception as e:
        _log(f'[kodi] ripresa fallita: {e}')


def _kodi_input_action(action):
    try:
        _kodi_rpc_request({
            'jsonrpc': '2.0',
            'method': 'Input.ExecuteAction',
            'params': {'action': action},
            'id': 1
        })
        _log(f'[kodi] Input.ExecuteAction: {action}')
    except Exception as e:
        _log(f'[kodi] Input.ExecuteAction({action}) fallita: {e}')


# ══════════════════════════════════════════════════════════════════════
#  UINPUT — Tastiera virtuale per iniezione tasti su Wayland
# ══════════════════════════════════════════════════════════════════════
import fcntl
import struct as _struct

_UI_SET_EVBIT  = 0x40045564
_UI_SET_KEYBIT = 0x40045565
_UI_DEV_CREATE = 0x5501
_EV_SYN_UI = 0x00
_EV_KEY_UI = 0x01
_SYN_REPORT = 0

# Codici evdev frecce e pagina
_UINPUT_ARROW_CODES = {
    'up':    103,
    'down':  108,
    'left':  105,
    'right': 106,
    'pageup':   104,
    'pagedown': 109,
}

_uinput_fd = None
_uinput_lock = threading.Lock()


def _uinput_setup():
    global _uinput_fd
    try:
        fd = open('/dev/uinput', 'wb', buffering=0)
        fcntl.ioctl(fd, _UI_SET_EVBIT, _EV_KEY_UI)
        for kc in _UINPUT_ARROW_CODES.values():
            fcntl.ioctl(fd, _UI_SET_KEYBIT, kc)
        # struct uinput_user_dev: 80s name + HHHHI + 256i abs arrays
        dev_data = _struct.pack(
            '80sHHHHI' + 'i' * 256,
            b'CV TV Arrows',
            0x03, 0, 0, 0,   # BUS_USB, vendor=0, product=0, version=0
            0,               # ff_effects_max
            *([0] * 256)     # absmax/min/fuzz/flat (4 × 64)
        )
        fd.write(dev_data)
        fcntl.ioctl(fd, _UI_DEV_CREATE)
        _uinput_fd = fd
        _log('[uinput] tastiera virtuale frecce creata')
    except Exception as e:
        _log(f'[uinput] setup error: {e}')
        _uinput_fd = None


def _uinput_send_key(key_code: int):
    """Invia press+release di un tasto via uinput (Wayland nativo)."""
    global _uinput_fd
    with _uinput_lock:
        fd = _uinput_fd
        if fd is None:
            return
        try:
            # struct input_event su 64-bit: ll HH i = 8+8+2+2+4 = 24 bytes
            def _ev(t, c, v):
                return _struct.pack('llHHi', 0, 0, t, c, v)
            fd.write(_ev(_EV_KEY_UI, key_code, 1))       # press
            fd.write(_ev(_EV_SYN_UI, _SYN_REPORT, 0))
            fd.write(_ev(_EV_KEY_UI, key_code, 0))       # release
            fd.write(_ev(_EV_SYN_UI, _SYN_REPORT, 0))
        except Exception as e:
            _log(f'[uinput] send_key error: {e}')
            _uinput_fd = None


# ══════════════════════════════════════════════════════════════════════
#  EVDEV KEY LISTENER v2
# ══════════════════════════════════════════════════════════════════════

_EV_KEY = 0x01
_pending_browser_back = False

_KEY_CFG = {}
_DEVICE_CFG = {}
try:
    for _k, _v in CFG.get('keys', {}).items():
        if not _k.startswith('_'):
            _KEY_CFG[int(_k)] = _v
    _DEVICE_CFG = CFG.get('device', {})
except Exception as _kerr:
    _log(f'[evdev] config keys parse error: {_kerr}')

_DEBOUNCE_SEC = float(_DEVICE_CFG.get('debounce_sec', 0.25))
_last_key_ts = {}


def _key_log(msg):
    try:
        line = f'[{time.strftime("%H:%M:%S")}] {msg}\n'
        with open(KEY_LOG_PATH, 'a') as f:
            f.write(line)
    except Exception:
        pass
    _log(f'[KEY] {msg}')


def _debounce_ok(code):
    now = time.time()
    if now - _last_key_ts.get(code, 0) < _DEBOUNCE_SEC:
        return False
    _last_key_ts[code] = now
    return True


_ARROW_CODES = set(_UINPUT_ARROW_CODES.values())  # 103,104,105,106,108,109

def _handle_key_action(code, value, dev_name):
    global _pending_browser_back, _pending_playpause, _pending_scroll
    if value == 0:
        return
    # Permetti key-repeat (value=2) per volume E per le frecce (scorrimento continuo)
    if value == 2 and code not in (114, 115) and code not in _ARROW_CODES:
        return

    cfg = _KEY_CFG.get(code)
    if cfg is None:
        return
    action = cfg.get('action', 'pass')
    name   = cfg.get('name', f'KEY_{code}')
    if action == 'pass':
        return

    if action not in ('vol_up', 'vol_down') and not _debounce_ok(code):
        return

    with _lock:
        app = _current_app
        hdmi = _current_hdmi

    _key_log(f'{name}(code={code}) action={action} app={app} hdmi={hdmi} dev={dev_name}')

    if action == 'tv':
        if code == 105 and hdmi == 'hdmi2':
            # LEFT key su Firestick = navigazione CEC sinistra
            threading.Thread(target=_cec_firestick_key, args=('left',), daemon=True).start()
        else:
            # HOME e tutti gli altri tasti tv = torna a ScreenTV
            threading.Thread(target=_home_action, daemon=True).start()

    elif action == 'kodi':
        threading.Thread(target=launch_kodi, daemon=True).start()

    elif action == 'back':
        if hdmi == 'hdmi2':
            # Su Firestick: BACK = CEC back alla Firestick
            threading.Thread(target=_cec_firestick_key, args=('back',), daemon=True).start()
        elif app == 'kodi':
            threading.Thread(target=_kodi_input_action, args=('back',), daemon=True).start()
        elif app in ('netflix', 'prime', 'disney'):
            threading.Thread(target=_streaming_back, daemon=True).start()
        elif app in ('browser', 'homeassistant'):
            with _lock:
                _pending_browser_back = True

    elif action == 'firestick':
        if hdmi == 'hdmi2':
            # Già su Firestick: RIGHT = navigazione CEC destra
            threading.Thread(target=_cec_firestick_key, args=('right',), daemon=True).start()
        else:
            threading.Thread(target=_switch_to_firestick, daemon=True).start()

    elif action == 'power':
        if app == 'kodi':
            threading.Thread(target=_kodi_power_off, daemon=True).start()
        else:
            threading.Thread(target=_power_toggle, daemon=True).start()

    elif action == 'playpause':
        if hdmi == 'hdmi2':
            threading.Thread(target=_cec_firestick_key, args=('play',), daemon=True).start()
        elif app == 'kodi':
            threading.Thread(target=_kodi_input_action, args=('playpause',), daemon=True).start()
        elif app in ('netflix', 'prime', 'disney', 'browser'):
            with _lock:
                _pending_playpause = True

    elif action in ('channel_up','channel_down','arrow_up','arrow_down','arrow_left','arrow_right'):
        dir_map = {'arrow_up': 'up', 'arrow_down': 'down', 'arrow_left': 'left', 'arrow_right': 'right'}
        code_map = {'arrow_up': 103, 'arrow_down': 108, 'arrow_left': 105, 'arrow_right': 106}
        if hdmi == 'hdmi2' and action in ('arrow_up', 'arrow_down'):
            # Su Firestick: frecce su/giù → CEC User Control alla Firestick
            btn = 'up' if action == 'arrow_up' else 'down'
            threading.Thread(target=_cec_firestick_key, args=(btn,), daemon=True).start()
        elif action in dir_map:
            uinput_code = code_map[action]
            threading.Thread(target=_uinput_send_key, args=(uinput_code,), daemon=True).start()
            if app in ('browser', 'homeassistant'):
                with _lock:
                    _pending_scroll = dir_map[action]
            _key_log(f'{name}: uinput key {uinput_code} → {dir_map[action]} (app={app})')
        else:
            _key_log(f'{name}: pass → sistema')

    elif action == 'select':
        if hdmi == 'hdmi2':
            threading.Thread(target=_cec_firestick_key, args=('select',), daemon=True).start()
        else:
            # Su ScreenTV/Kodi: ENTER come tasto uinput
            threading.Thread(target=_uinput_send_key, args=(28,), daemon=True).start()

    elif action == 'vol_up':
        if hdmi == 'hdmi2':
            # Su Firestick: CEC Volume Up alla TV (80=Pi→TV, 44=UCP, 41=Vol+)
            _cec_send('tx 80:44:41')
            time.sleep(0.08)
            _cec_send('tx 80:45')
            _key_log('CEC: Volume Up → TV')
        else:
            subprocess.run(['wpctl','set-volume','@DEFAULT_AUDIO_SINK@','5%+'], capture_output=True)
            _show_wob()

    elif action == 'vol_down':
        if hdmi == 'hdmi2':
            # Su Firestick: CEC Volume Down alla TV (42=Vol-)
            _cec_send('tx 80:44:42')
            time.sleep(0.08)
            _cec_send('tx 80:45')
            _key_log('CEC: Volume Down → TV')
        else:
            subprocess.run(['wpctl','set-volume','@DEFAULT_AUDIO_SINK@','5%-'], capture_output=True)
            _show_wob()

    elif action == 'mute':
        subprocess.run(['wpctl','set-mute','@DEFAULT_AUDIO_SINK@','toggle'], capture_output=True)
        _show_wob(muted=True)


_WOB_PIPE = '/tmp/wobpipe'
_wob_fd = None


def _wob_open():
    global _wob_fd
    try:
        if not os.path.exists(_WOB_PIPE):
            os.mkfifo(_WOB_PIPE)
        if _wob_fd is not None:
            try:
                os.close(_wob_fd)
            except Exception:
                pass
            _wob_fd = None
        _wob_fd = os.open(_WOB_PIPE, os.O_WRONLY | os.O_NONBLOCK)
        _log(f'[wob] fd aperto: {_wob_fd}')
    except OSError as e:
        import errno as _errno2
        if getattr(e, 'errno', None) == _errno2.ENXIO:
            _wob_fd = None
        else:
            _log(f'[wob] open error: {e}')
    except Exception as e:
        _log(f'[wob] open error: {e}')


def _show_wob(muted=False):
    global _wob_fd
    import re as _re2, errno as _errno2

    try:
        if muted:
            val = 0
        else:
            r = subprocess.run(
                ['wpctl', 'get-volume', '@DEFAULT_AUDIO_SINK@'],
                capture_output=True, text=True, timeout=1
            )
            m = _re2.search(r'[0-9]+\.[0-9]+', r.stdout)
            if not m:
                return
            val = min(100, int(float(m.group()) * 100))

        if _wob_fd is None:
            _wob_open()
        if _wob_fd is None:
            return

        try:
            os.write(_wob_fd, f'{val}\n'.encode())
            _log(f'[wob] {val}%{" (muted)" if muted else ""}')
        except BlockingIOError:
            pass
        except OSError as e2:
            if getattr(e2, 'errno', None) in (_errno2.EPIPE, _errno2.EBADF, _errno2.ENXIO):
                _log(f'[wob] fd broken ({e2.errno}), reset')
                try:
                    os.close(_wob_fd)
                except Exception:
                    pass
                _wob_fd = None
    except Exception as e:
        _log(f'[wob] errore: {e}')


def _cec_tv_on_after_kodi():
    global _user_tv_off
    if _user_tv_off:
        _key_log("HOME: TV spenta dall'utente — non la riaccendo")
        return
    try:
        with _cec_lock:
            # Kodi può mandare standby al termine; ri-dichiara active source
            _cec_send('as')
        _key_log('HOME: CEC as → Raspberry active source ripristinato dopo Kodi')
    except Exception as e:
        _key_log(f'HOME: CEC ripristino TV errore: {e}')


def _home_action():
    _key_log('HOME: chiudo Kodi e torno a screenTV...')
    close_browser()
    close_homeassistant()
    close_netflix()
    close_prime_video()
    close_disney_plus()
    _stop_kodi_hard()
    with _lock:
        globals()['_current_app'] = 'tv'
        globals()['_current_hdmi'] = 'hdmi1'
    # Switch TV a HDMI-1 (Pi): invia as solo qui, non al boot
    with _cec_lock:
        _cec_send('as')
    _key_log('HOME: CEC as → TV su HDMI-1 (ScreenTV)')
    time.sleep(0.3)
    _key_log('HOME: focus screenTV')
    _focus_tv()


# ── Switch input CEC e controllo Firestick ────────────────────
# Firestick è su HDMI-2 → physical address CEC 2.0.0.0 (header tx: 8f:86:20:00)
# Firestick logical address = 4 (Playback Device 1), header Pi→FS = 84
_FIRESTICK_PHYS = '2000'   # hex CEC phys addr

# CEC User Control codes per Firestick
_FS_BTN = {
    'up': '01', 'down': '02', 'left': '03', 'right': '04',
    'select': '00', 'back': '0d', 'home': '09',
    'play': '44', 'pause': '46',
}

def _cec_firestick_key(btn: str):
    """Invia CEC User Control Pressed + Released alla Firestick (LA 4).
    84 = header da Pi (LA 8) a Firestick (LA 4)."""
    code = _FS_BTN.get(btn, btn)
    _cec_send(f'tx 84:44:{code}')
    time.sleep(0.08)
    _cec_send('tx 84:45')
    _key_log(f'CEC Firestick: {btn} ({code})')


def _switch_to_firestick():
    global _current_hdmi
    with _lock:
        globals()['_current_hdmi'] = 'hdmi2'
    # tx 8f:86:20:00 = Set Stream Path broadcast verso 2.0.0.0 (Firestick HDMI-2)
    # La Firestick risponde con Active Source, la TV switcha su HDMI-2.
    with _cec_lock:
        _cec_send('tx 8f:86:20:00')
    _key_log('CEC: switch → Firestick HDMI-2 (tx Set Stream Path 2.0.0.0)')


# ── Power toggle via sessione CEC persistente ─────────────────
# Con la sessione persistente il bus CEC non viene re-inizializzato
# ad ogni comando, quindi non si ha più il problema dell'auto-on 0.
# La logica è semplice: query pow 0 → se TV è on manda standby, altrimenti on.
# Per gestire gli stati di transizione:
#   "in transition from standby to on" → TV si sta accendendo → trattala come ON
#   tutto il resto (standby, unknown, transizione verso standby) → manda ON


def _power_toggle():
    """Toggle ON/OFF TV via CEC. Usa stato interno _user_tv_off per risposta
    immediata senza query — zero latenza dal tasto alla TV."""
    global _user_tv_off
    if not _cec_lock.acquire(blocking=False):
        _key_log('POWER TOGGLE: CEC occupato, ignorato')
        return
    try:
        # Azione immediata basata sullo stato interno (nessuna query pow 0)
        tv_was_on = not _user_tv_off
        _key_log(f'POWER TOGGLE: stato interno = {"ON" if tv_was_on else "STANDBY"}')
        if tv_was_on:
            # Philips EasyLink spesso ignora standby diretto (LA 0).
            # Broadcast standby (LA 15) raggiunge tutti i dispositivi inclusa la TV.
            _cec_send('standby 0')   # prima diretto, per sicurezza
            time.sleep(0.1)
            _cec_send('standby f')   # poi broadcast (più efficace su Philips)
            _user_tv_off = True
            _key_log('CEC: TV → STANDBY (standby 0 + broadcast 15)')
        else:
            # Wake TV: on 0 (Image View On) + ripristina l'input corretto
            _cec_send('on 0')
            time.sleep(0.3)
            if _current_hdmi == 'hdmi2':
                _cec_send(f'sp {_FIRESTICK_PHYS}')
                _key_log(f'CEC: TV → ON + sp {_FIRESTICK_PHYS} (Firestick HDMI-2)')
            else:
                _cec_send('as')
                _key_log('CEC: TV → ON + as (ScreenTV HDMI-1)')
            _user_tv_off = False
    except Exception as e:
        _key_log(f'POWER TOGGLE errore: {e}')
    finally:
        _cec_lock.release()


def _cec_init_tv_state():
    """Interroga stato reale TV all'avvio e aggiorna _user_tv_off.
    Se la TV è accesa dichiara active source; se è in standby NON manda as
    (evita di svegliare la TV dopo che l'utente l'ha spenta e il proxy è ripartito)."""
    global _user_tv_off
    # Aspetta che la sessione CEC sia pronta
    time.sleep(2)
    status = _cec_query_power(timeout=6.0)
    _log(f'[cec] stato TV all\'avvio: "{status}"')
    if status in ('on', 'in transition from standby to on'):
        _user_tv_off = False
        # NON mandiamo as: non vogliamo interrompere l'utente se è sulla Firestick
        _log('[cec] TV accesa → as NON inviato (non interrompo input attivo)')
    elif status in ('standby', 'in transition from on to standby'):
        _user_tv_off = True
        _log('[cec] TV in standby → as NON inviato')
    else:
        _user_tv_off = False
        _log(f'[cec] stato unknown → _user_tv_off = False')
    _log(f'[cec] _user_tv_off inizializzato = {_user_tv_off}')


def _kodi_power_off():
    """Spegne TV via CEC PRIMA di killare Kodi: la TV va buia immediatamente
    senza mostrare la UI di Kodi durante la chiusura."""
    global _user_tv_off
    _key_log('KODI POWER OFF: spengo TV e chiudo Kodi...')
    _user_tv_off = True
    try:
        with _cec_lock:
            _cec_send('standby 0')
            time.sleep(0.1)
            _cec_send('standby f')
        _key_log('KODI POWER OFF: CEC standby 0+f → TV spenta')
    except Exception as e:
        _key_log(f'KODI POWER OFF: errore CEC: {e}')
    # Kodi viene killato DOPO che la TV è già in standby
    _stop_kodi_hard()
    with _lock:
        globals()['_current_app'] = 'tv'


def _streaming_playpause():
    for cmd in [['ydotool','key','57:1','57:0'], ['xdotool','key','space']]:
        try:
            if subprocess.run(cmd, env=_wenv(), capture_output=True, timeout=2).returncode == 0:
                _key_log(f'Streaming playpause: {cmd[0]} OK')
                return
        except FileNotFoundError:
            continue
        except Exception as e:
            _key_log(f'Streaming playpause {cmd[0]}: {e}')


def _streaming_back():
    for cmd in [['ydotool','key','56:1','105:1','105:0','56:0'], ['xdotool','key','alt+Left']]:
        try:
            if subprocess.run(cmd, env=_wenv(), capture_output=True, timeout=2).returncode == 0:
                _key_log(f'Streaming back: {cmd[0]} OK')
                return
        except FileNotFoundError:
            continue
        except Exception as e:
            _key_log(f'Streaming back {cmd[0]}: {e}')


# ── FIX #2: Device listener con exit pulito per watchdog ──────
def _read_input_device(dev_path, dev_name):
    import platform
    is64 = platform.architecture()[0] == '64bit'
    size, fmt = (24, 'llHHi') if is64 else (16, 'iiHHi')
    _key_log(f'Listener: {dev_path} ({dev_name})')

    with _active_listener_lock:
        _active_listener_devices.add(dev_path)

    fd = None
    try:
        try:
            fd = open(dev_path, 'rb')
        except Exception as e:
            _key_log(f'ERRORE apertura {dev_path}: {e}')
            return

        while True:
            try:
                data = fd.read(size)
                if not data or len(data) < size:
                    # Device disconnesso (sleep batteria o unplug)
                    _key_log(f'[listener] {dev_path} disconnesso (read vuoto) — uscita thread')
                    break
                ev_type, ev_code, ev_value = struct.unpack(fmt, data)[2:5]
                if ev_type == _EV_KEY:
                    _handle_key_action(ev_code, ev_value, dev_name)
            except Exception as e:
                _key_log(f'[listener] Errore {dev_path}: {e} — uscita thread')
                break
    finally:
        if fd:
            try:
                fd.close()
            except Exception:
                pass
        with _active_listener_lock:
            _active_listener_devices.discard(dev_path)
        _key_log(f'[listener] Thread terminato per {dev_path}')


def _find_airmouse_devices():
    prefix = _DEVICE_CFG.get('name', 'AirMouse').strip().lower()
    found = []
    try:
        with open('/proc/bus/input/devices') as f:
            content = f.read()
        cur = {}
        for line in content.splitlines():
            if line.startswith('I:'):
                cur = {}
            elif line.startswith('N:'):
                cur['name'] = line.split('=',1)[1].strip().strip('"').strip()
            elif line.startswith('H: Handlers='):
                for h in line.split('=',1)[1].split():
                    if h.startswith('event'):
                        cur['dev'] = f'/dev/input/{h}'
                        break
            elif not line.strip() and cur.get('dev'):
                n = cur.get('name','').lower()
                if n.startswith(prefix) or prefix in n:
                    found.append((cur['dev'], cur.get('name','')))
                cur = {}
    except Exception as e:
        _key_log(f'find_devices errore: {e}')
    _key_log(f'Device trovati ({prefix}): {found}')
    return found


def _device_watchdog():
    """
    FIX #2: Ogni 8 secondi scansiona i device AirMouse.
    Se trova un device non ancora monitorato (nuovo eventX dopo riconnessione),
    avvia un nuovo thread listener. I thread precedenti escono da soli
    quando il device si disconnette (read restituisce vuoto).
    """
    _key_log('[watchdog] Avviato watchdog device AirMouse')
    while True:
        time.sleep(8)
        try:
            devs = _find_airmouse_devices()
            with _active_listener_lock:
                active = set(_active_listener_devices)
            for dev_path, dev_name in devs:
                if dev_path not in active:
                    _key_log(f'[watchdog] Nuovo device rilevato: {dev_path} ({dev_name}) — avvio listener')
                    threading.Thread(
                        target=_read_input_device,
                        args=(dev_path, dev_name),
                        daemon=True
                    ).start()
        except Exception as e:
            _key_log(f'[watchdog] errore: {e}')


def start_input_listener():
    _key_log(f'Mappa tasti: {len(_KEY_CFG)} entries')
    for code, cfg in sorted(_KEY_CFG.items()):
        _key_log(f'  {code:4d} {cfg.get("name","?"):20s} → {cfg.get("action","?")}')

    explicit = INPUT_DEVICE.strip() if 'INPUT_DEVICE' in dir() else ''
    if explicit:
        _key_log(f'Device esplicito: {explicit}')
        threading.Thread(target=_read_input_device, args=(explicit,'explicit'), daemon=True).start()
        return

    devs = _find_airmouse_devices()
    if not devs:
        _key_log('NESSUN DEVICE — aggiungi input_device in cv-tv-config.json')
    else:
        for dev_path, dev_name in devs:
            threading.Thread(target=_read_input_device, args=(dev_path, dev_name), daemon=True).start()

    # FIX #2: avvia watchdog indipendentemente (gestisce anche il caso "nessun device all'avvio")
    threading.Thread(target=_device_watchdog, daemon=True).start()


def launch_tv():
    global _current_app

    with _lock:
        _current_app = 'tv'

    close_browser()
    close_homeassistant()
    close_netflix()
    close_prime_video()
    close_disney_plus()
    _stop_kodi_hard()

    threading.Thread(target=_focus_tv, daemon=True).start()


# ── HLS Proxy (webcam) ────────────────────────────────────────

def is_allowed(url):
    try:
        host = urllib.parse.urlparse(url).hostname or ''
        return any(host == a or host.endswith('.' + a) for a in ALLOWED)
    except Exception:
        return False


def rewrite_m3u8(body, original_url):
    base = original_url.rsplit('/', 1)[0] + '/'
    out = []

    for line in body.decode('utf-8', errors='replace').splitlines():
        s = line.strip()
        if s == '' or s.startswith('#'):
            out.append(line)
        else:
            abs_url = s if s.startswith('http') else base + s
            out.append(PROXY_BASE + urllib.parse.quote(abs_url, safe=''))

    return ('\n'.join(out) + '\n').encode('utf-8')


def _extract_cam_id(url):
    m = re.search(r'/live/(\d+)/', url)
    if m:
        return m.group(1)
    for cid, (cached_url, _) in list(_token_cache.items()):
        if cid in url:
            return cid
    webcams = CFG.get('webcams', {})
    return next((k for k in webcams if k in url), None)


def resolve_cam(cam_id):
    now = time.time()

    if cam_id in _token_cache and now - _token_cache[cam_id][1] < TOKEN_TTL:
        return _token_cache[cam_id][0]

    pages = CFG.get('webcams', {})
    page_url = pages.get(str(cam_id))
    if not page_url:
        return None

    try:
        req = urllib.request.Request(
            page_url,
            headers={'User-Agent': FAKE_HEADERS['User-Agent'], 'Accept': 'text/html'}
        )
        html = urllib.request.urlopen(req, timeout=10).read().decode('utf-8', errors='replace')
    except Exception as e:
        _log(f'[resolve] fetch failed: {e}')
        return None

    m = re.search(r"source\s*:\s*'livee\.m3u8\?a=([^']+)'", html)
    if not m:
        _log(f'[resolve] token not found cam={cam_id}')
        return None

    token = m.group(1)
    real_url = f'https://hd-auth.skylinewebcams.com/live.m3u8?a={token}'
    proxied = PROXY_BASE + urllib.parse.quote(real_url, safe='')
    _token_cache[cam_id] = (proxied, now)

    _log(f'[resolve] cam={cam_id} OK')
    return proxied


# ── HTTP Handler ──────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        _log(f'[http] {self.address_string()} {fmt % args}')

    def do_GET(self):
        global _pending_command, _pending_scroll, _pending_browser_back, _pending_playpause

        p = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(p.query)

        if p.path == '/health':
            self._j(200, {
                'ok': True,
                'version': '8.5',
                'app': _current_app,
                'wayland': WAYLAND_DISPLAY,
                'keyboard': keyboard_state(),
                'toplevels': _toplevels()[:300],
                'cec_pid': _cec_proc.pid if _cec_proc and _cec_proc.poll() is None else None,
                'active_listeners': sorted(_active_listener_devices),
            })

        elif p.path == '/status':
            self._j(200, {'app': _current_app, 'keyboard': keyboard_state()})

        elif p.path == '/launch/kodi':
            self._r(200, b'<html><body></body></html>', 'text/html')
            threading.Thread(target=launch_kodi, daemon=True).start()

        elif p.path == '/launch/tv':
            self._r(200, b'<html><body></body></html>', 'text/html')
            threading.Thread(target=launch_tv, daemon=True).start()

        elif p.path == '/launch/netflix':
            setup = qs.get('setup', ['0'])[0].lower() in ('1', 'true', 'yes')
            html = b'<html><head><meta http-equiv="refresh" content="0"></head><body></body></html>'
            self._r(200, html, 'text/html')
            threading.Thread(target=launch_netflix, args=(setup,), daemon=True).start()

        elif p.path == '/launch/prime':
            setup = qs.get('setup', ['0'])[0].lower() in ('1', 'true', 'yes')
            html = b'<html><head><meta http-equiv="refresh" content="0"></head><body></body></html>'
            self._r(200, html, 'text/html')
            threading.Thread(target=launch_prime_video, args=(setup,), daemon=True).start()

        elif p.path == '/launch/disney':
            setup = qs.get('setup', ['0'])[0].lower() in ('1', 'true', 'yes')
            html = b'<html><head><meta http-equiv="refresh" content="0"></head><body></body></html>'
            self._r(200, html, 'text/html')
            threading.Thread(target=launch_disney_plus, args=(setup,), daemon=True).start()

        elif p.path == '/poll':
            cmd = _pending_command
            _pending_command = None
            if cmd:
                self._j(200, {'command': cmd})
            else:
                self._j(200, {'command': None})

        elif p.path == '/launch/back':
            _pending_command = 'back'
            self._j(200, {'ok': True, 'action': 'back'})

        elif p.path == '/launch/keyboard':
            action = qs.get('action', ['show'])[0]
            if action == 'hide':
                hide_keyboard()
                self._j(200, {'ok': True, 'keyboard': 'hidden'})
            else:
                threading.Thread(target=show_keyboard, daemon=True).start()
                self._j(200, {'ok': True, 'keyboard': 'shown'})

        elif p.path == '/launch/homeassistant':
            self._j(200, {'ok': True, 'launching': HA_URL})
            threading.Thread(target=launch_homeassistant, daemon=True).start()

        elif p.path == '/launch/browser':
            url = qs.get('url', ['https://www.google.com'])[0]
            self._j(200, {'ok': True, 'launching': url})
            threading.Thread(target=launch_browser, args=(url,), daemon=True).start()

        elif p.path == '/launch/browser/home':
            with _lock:
                app = _current_app
            # Non interrompere Netflix/Prime/Disney/Kodi: il tasto home sulla FAB
            # o il polling della extension non devono chiudere lo streaming.
            if app in ('netflix', 'prime', 'disney', 'kodi'):
                self._j(200, {'ok': False, 'action': 'ignored', 'app': app})
            else:
                self._j(200, {'ok': True, 'action': 'go_home'})
                threading.Thread(target=launch_tv, daemon=True).start()

        elif p.path == '/remote/arrow':
            direction = qs.get('dir', [''])[0].lower()
            if direction not in ('up', 'down', 'left', 'right'):
                self._j(400, {'error': 'dir must be up|down|left|right'})
            else:
                with _lock:
                    app = _current_app
                if app == 'kodi':
                    action_map = {
                        'up':    'channelup',
                        'down':  'channeldown',
                        'left':  'left',
                        'right': 'right',
                    }
                    kodi_action = action_map[direction]
                    threading.Thread(
                        target=_kodi_input_action,
                        args=(kodi_action,),
                        daemon=True
                    ).start()
                    self._j(200, {'ok': True, 'app': 'kodi', 'action': kodi_action})
                else:
                    if app == 'browser' and direction:
                        with _lock:
                            _pending_scroll = direction
                    self._j(200, {'ok': True, 'app': app, 'action': 'none'})

        elif p.path == '/remote/browser-commands':
            with _lock:
                cmd = _pending_scroll
                do_back = _pending_browser_back
                do_pp = _pending_playpause
                app = _current_app
                _pending_scroll = None
                _pending_browser_back = False
                _pending_playpause = False
            self._j(200, {'scroll': cmd, 'back': do_back, 'playpause': do_pp, 'app': app})

        elif p.path == '/remote/fab-position':
            host = qs.get('host', [''])[0]
            pos = _FAB_POSITIONS.get(host, _FAB_POSITIONS.get('default', {'top': 16, 'right': 16}))
            self._j(200, pos)

        elif p.path == '/remote/scroll-command':
            with _lock:
                cmd = _pending_scroll
                _pending_scroll = None
            self._j(200, {'scroll': cmd})

        elif p.path == '/remote/back-command':
            with _lock:
                do_back = _pending_browser_back
                _pending_browser_back = False
            self._j(200, {'back': do_back})

        elif p.path == '/remote/playpause-command':
            with _lock:
                do_pp = _pending_playpause
                _pending_playpause = False
            self._j(200, {'playpause': do_pp})

        elif p.path == '/resolve':
            cam = qs.get('id', [''])[0]
            url = resolve_cam(cam)
            self._j(200 if url else 502, {'url': url} if url else {'error': 'token not found'})

        elif p.path == '/stream':
            target = qs.get('url', [''])[0]
            if not target:
                self._r(400, b'Missing ?url=', 'text/plain')
                return

            if not is_allowed(target):
                self._r(403, b'Host not allowed', 'text/plain')
                return

            effective_target = target
            if 'skylinewebcams.com' in target and '/live/' in target:
                cam_id = _extract_cam_id(target)
                if cam_id:
                    cached = _token_cache.get(cam_id)
                    if cached:
                        cached_url, _ = cached
                        m_target = re.search(r'[?&]a=([^&]+)', target)
                        m_cached = re.search(r'[?&]a=([^&]+)', cached_url)
                        if m_target and m_cached and m_target.group(1) != m_cached.group(1):
                            effective_target = cached_url.split('/stream?url=')[-1]
                            effective_target = urllib.parse.unquote(effective_target)

            try:
                req = urllib.request.Request(effective_target, headers=FAKE_HEADERS)
                resp = urllib.request.urlopen(req, timeout=10)
                body = resp.read()
                ct = resp.headers.get('Content-Type', 'application/octet-stream')

                if 'mpegurl' in ct.lower() or effective_target.split('?')[0].endswith('.m3u8'):
                    body = rewrite_m3u8(body, effective_target)
                    ct = 'application/vnd.apple.mpegurl'

                self._r(200, body, ct)

            except urllib.error.HTTPError as e:
                if e.code in (401, 403) and 'skylinewebcams.com' in effective_target:
                    cam_id = _extract_cam_id(effective_target)
                    if cam_id:
                        _log(f'[stream] HTTP {e.code} — token scaduto cam={cam_id}, refresh...')
                        _token_cache.pop(cam_id, None)
                        fresh_proxied = resolve_cam(cam_id)
                        if fresh_proxied:
                            try:
                                fresh_url = urllib.parse.unquote(
                                    fresh_proxied.split('/stream?url=')[-1]
                                )
                                req2 = urllib.request.Request(fresh_url, headers=FAKE_HEADERS)
                                resp2 = urllib.request.urlopen(req2, timeout=10)
                                body2 = resp2.read()
                                ct2 = resp2.headers.get('Content-Type', 'application/vnd.apple.mpegurl')
                                if 'mpegurl' in ct2.lower() or fresh_url.split('?')[0].endswith('.m3u8'):
                                    body2 = rewrite_m3u8(body2, fresh_url)
                                    ct2 = 'application/vnd.apple.mpegurl'
                                self._r(200, body2, ct2)
                                return
                            except Exception as e2:
                                _log(f'[stream] refresh fallito: {e2}')
                self._r(e.code, str(e).encode(), 'text/plain')
            except Exception as e:
                self._r(502, str(e).encode(), 'text/plain')

        else:
            self._r(404, b'Not found', 'text/plain')

    def _j(self, code, data):
        self._r(code, json.dumps(data).encode(), 'application/json')

    def _r(self, code, body, ct):
        try:
            self.send_response(code)
            self.send_header('Content-Type', ct)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Private-Network', 'true')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, HEAD, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-CV-Token')
        self.send_header('Access-Control-Allow-Private-Network', 'true')
        self.send_header('Access-Control-Max-Age', '600')
        self.end_headers()


if __name__ == '__main__':
    _uinput_setup()
    start_input_listener()
    threading.Thread(target=_wob_open, daemon=True).start()

    _log(f'[proxy] v8.4 porta={PORT} wayland={WAYLAND_DISPLAY}')
    ok, tl = _wlrctl('toplevel', 'list')
    _log(f'[proxy] wlrctl={ok} toplevels: {tl[:100] or "(nessuno)"}')

    # Avvia sessione CEC persistente e dichiara Raspberry active source
    _cec_start()
    threading.Thread(target=_cec_watchdog, daemon=True).start()
    # Aspetta che cec-client sia pronto (riga "waiting for input")
    _log('[cec] attendo inizializzazione...')
    _deadline = time.time() + 12
    while time.time() < _deadline:
        _cec_out_event.wait(timeout=0.5)
        _cec_out_event.clear()
        if any('waiting for input' in ln for _, ln in _cec_out_lines):
            break
    # Interroga stato TV in background: se è accesa dichiara active source,
    # se è in standby NON mandiamo as (evita di svegliarla al riavvio del proxy)
    threading.Thread(target=_cec_init_tv_state, daemon=True).start()

    srv = http.server.HTTPServer(('127.0.0.1', PORT), Handler)
    srv.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        _log('[proxy] Fermato.')
