import os, sys, time, datetime, sqlite3, secrets, shutil, threading
import subprocess, zipfile, html, json
from flask import (Flask, request, render_template_string, session,
                   redirect, url_for, send_from_directory, abort)
from functools import wraps
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

# ---------------- PATHS ----------------
DATA_DIR = os.environ.get("DATA_DIR", "/data")
if not os.path.isdir(DATA_DIR) or not os.access(DATA_DIR, os.W_OK):
    DATA_DIR = os.path.abspath("local_data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH    = os.path.join(DATA_DIR, "site.db")
os.makedirs(UPLOAD_DIR, exist_ok=True)

MAX_UPLOAD_MB = 200
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

ADMIN_PATH         = "/x-admin-7k9p2"
DEFAULT_ADMIN_CODE = "1298"
SITE_URL_ENV       = os.environ.get("SITE_URL", "").rstrip("/")

# ---------------- DATABASE ----------------
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def _alter(c, tbl, col, typ):
    try: c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {typ}")
    except sqlite3.OperationalError: pass

def init_db():
    conn = db(); c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, ip TEXT,
        first_seen REAL, last_seen REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS access_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE,
        max_uses INTEGER, used INTEGER DEFAULT 0, expires_at REAL,
        active INTEGER DEFAULT 1, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT, orig TEXT,
        owner_code TEXT, owner_ip TEXT, size INTEGER,
        uploaded_at TEXT, extract_path TEXT, install_job_id INTEGER,
        source TEXT DEFAULT 'web')""")
    c.execute("""CREATE TABLE IF NOT EXISTS user_details (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT,
        note TEXT, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS install_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, file_id INTEGER,
        status TEXT, log TEXT, started_at TEXT, finished_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS bots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT, admin_chat TEXT,
        secret TEXT UNIQUE, active INTEGER DEFAULT 0,
        webhook_url TEXT, created_at TEXT, bot_username TEXT)""")
    _alter(c, 'files', 'extract_path', 'TEXT')
    _alter(c, 'files', 'install_job_id', 'INTEGER')
    _alter(c, 'files', 'source', "TEXT DEFAULT 'web'")
    defaults = {
        "admin_code": DEFAULT_ADMIN_CODE, "site_expiry": "0",
        "default_max_uses": "10", "default_valid_hours": "24",
        "telegram_token": "", "telegram_chat_id": "", "telegram_enabled": "0",
        "auto_install_on_startup": "1",
    }
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO config VALUES (?,?)", (k, v))
    conn.commit(); conn.close()

init_db()

def cfg(key, default=""):
    conn = db()
    r = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    conn.close()
    return r["value"] if r else default

def set_cfg(key, val):
    conn = db()
    conn.execute("INSERT OR REPLACE INTO config VALUES (?,?)", (key, str(val)))
    conn.commit(); conn.close()

def site_expired():
    try: exp = int(cfg("site_expiry", "0"))
    except: exp = 0
    return exp != 0 and time.time() > exp

# ---------------- TELEGRAM NOTIFICATIONS ----------------
def send_telegram(text):
    if cfg("telegram_enabled", "0") != "1": return
    token = cfg("telegram_token", "").strip()
    chat  = cfg("telegram_chat_id", "").strip()
    if not token or not chat: return
    try:
        import requests
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=6)
    except Exception: pass

# ---------------- INSTALL JOBS ----------------
install_lock = threading.Lock()

def create_job(kind, file_id=None, initial_log=""):
    conn = db()
    cur = conn.execute("INSERT INTO install_jobs (kind,file_id,status,log,started_at) "
                       "VALUES (?,?,?,?,?)",
                       (kind, file_id, "pending", initial_log,
                        datetime.datetime.now().isoformat()))
    jid = cur.lastrowid
    conn.commit(); conn.close()
    return jid

def append_job_log(jid, line):
    conn = db()
    r = conn.execute("SELECT log FROM install_jobs WHERE id=?", (jid,)).fetchone()
    if r:
        new = (r["log"] or "") + str(line) + "\n"
        if len(new) > 300_000: new = new[-300_000:]
        conn.execute("UPDATE install_jobs SET log=? WHERE id=?", (new, jid))
        conn.commit()
    conn.close()

def set_job_status(jid, status):
    conn = db()
    if status in ("success", "failed", "skipped"):
        conn.execute("UPDATE install_jobs SET status=?, finished_at=? WHERE id=?",
                     (status, datetime.datetime.now().isoformat(), jid))
    else:
        conn.execute("UPDATE install_jobs SET status=? WHERE id=?", (status, jid))
    conn.commit(); conn.close()

def run_install_job(jid, req_path, workdir, label="install"):
    with install_lock:
        set_job_status(jid, "running")
        append_job_log(jid, "─" * 62)
        append_job_log(jid, f"▶️  Install started  ·  {label}")
        append_job_log(jid, f"📁 Workdir  : {workdir}")
        append_job_log(jid, f"📄 Requires : {req_path}")
        append_job_log(jid, "─" * 62)
        send_telegram(f"📦 <b>Install started</b>\n<code>{html.escape(label)}</code>")
        try:
            cmd = [sys.executable, "-m", "pip", "install",
                   "--no-cache-dir", "--disable-pip-version-check",
                   "-r", req_path]
            append_job_log(jid, "$ " + " ".join(cmd))
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    cwd=workdir, bufsize=1)
            for line in proc.stdout:
                append_job_log(jid, line.rstrip())
            proc.wait()
            append_job_log(jid, "─" * 62)
            if proc.returncode == 0:
                set_job_status(jid, "success")
                append_job_log(jid, "✅ INSTALL SUCCESSFUL")
                send_telegram(f"✅ <b>Install success</b>\n<code>{html.escape(label)}</code>")
            else:
                set_job_status(jid, "failed")
                append_job_log(jid, f"❌ FAILED (exit {proc.returncode})")
                send_telegram(f"❌ <b>Install failed</b> ({proc.returncode})")
        except Exception as e:
            set_job_status(jid, "failed")
            append_job_log(jid, f"❌ Exception: {e}")

def find_requirements(folder):
    for root, _, files in os.walk(folder):
        if "requirements.txt" in files:
            return os.path.join(root, "requirements.txt")
    return None

def is_safe_path(base, target):
    base = os.path.realpath(base); target = os.path.realpath(target)
    return target.startswith(base + os.sep) or target == base

def process_uploaded_file(file_id, path, filename):
    jid = create_job("upload", file_id, f"📁 File received: {filename}\n")
    conn = db()
    conn.execute("UPDATE files SET install_job_id=? WHERE id=?", (jid, file_id))
    conn.commit(); conn.close()
    name = filename.lower()
    if name.endswith(".zip"):
        extract_dir = os.path.join(UPLOAD_DIR, f"extract_{file_id}")
        os.makedirs(extract_dir, exist_ok=True)
        try:
            with zipfile.ZipFile(path, "r") as zf:
                zf.extractall(extract_dir)
            append_job_log(jid, f"📦 ZIP extracted → {extract_dir}")
            conn = db()
            conn.execute("UPDATE files SET extract_path=? WHERE id=?",
                         (extract_dir, file_id))
            conn.commit(); conn.close()
            req = find_requirements(extract_dir)
            if req and is_safe_path(UPLOAD_DIR, req):
                append_job_log(jid, f"✅ requirements.txt found → {req}")
                threading.Thread(target=run_install_job,
                                 args=(jid, req, os.path.dirname(req), filename),
                                 daemon=True).start()
            else:
                append_job_log(jid, "ℹ️  No requirements.txt inside ZIP.")
                append_job_log(jid, "⏭  Hosted as-is.")
                set_job_status(jid, "skipped")
        except Exception as e:
            append_job_log(jid, f"❌ ZIP error: {e}")
            set_job_status(jid, "failed")
    elif name == "requirements.txt":
        append_job_log(jid, "📄 requirements.txt detected — installing…")
        threading.Thread(target=run_install_job,
                         args=(jid, path, os.path.dirname(path), filename),
                         daemon=True).start()
    else:
        append_job_log(jid, "ℹ️  No requirements.txt — file hosted as-is.")
        set_job_status(jid, "skipped")

# ---------------- STARTUP AUTO INSTALL ----------------
def startup_auto_install():
    jid = create_job("startup", None, "🚀 PyHost auto-install check…\n")
    base = os.path.dirname(os.path.abspath(__file__))
    req = os.path.join(base, "requirements.txt")
    if not os.path.exists(req):
        append_job_log(jid, "ℹ️  No requirements.txt.")
        set_job_status(jid, "skipped"); return
    run_install_job(jid, req, base, "startup")

def _delayed_startup():
    time.sleep(2)
    try:
        if cfg("auto_install_on_startup", "1") == "1":
            startup_auto_install()
        else:
            jid = create_job("startup", None, "")
            append_job_log(jid, "⏸  Auto-install disabled.")
            set_job_status(jid, "skipped")
    except Exception as e:
        print(f"startup error: {e}")

threading.Thread(target=_delayed_startup, daemon=True).start()

# ---------------- AUTH ----------------
def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("admin"):
            return redirect(ADMIN_PATH + "/login")
        return f(*a, **k)
    return w

def user_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("user_ok"):
            return redirect(url_for("index"))
        code = session.get("user_code")
        if code:
            conn = db()
            r = conn.execute("SELECT * FROM access_codes WHERE code=? AND active=1",
                             (code,)).fetchone()
            conn.close()
            if not r:
                session.clear(); return redirect(url_for("index", err="invalid"))
            if r["expires_at"] and time.time() > r["expires_at"]:
                session.clear(); return redirect(url_for("index", err="expired"))
        return f(*a, **k)
    return w

@app.before_request
def gate():
    if request.path.startswith(ADMIN_PATH): return
    if request.path in ("/expired", "/healthz"): return
    if request.path.startswith("/bot-hook/"): return
    if site_expired(): return redirect("/expired")

# ---------------- UI (ANIMATED) ----------------
BASE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }}</title>
<style>
:root{--bg:#0a0f1a;--card:#131c2e;--card2:#0f172a;--line:#1f2d45;
--accent:#38bdf8;--accent2:#0ea5e9;--green:#10b981;--red:#ef4444;
--amber:#f59e0b;--muted:#94a3b8;--txt:#e2e8f0;--purple:#a78bfa}
*{box-sizing:border-box;margin:0;padding:0;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
@keyframes fadeIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}
@keyframes shimmer{0%{background-position:-200% 0}100%{background-position:200% 0}}
@keyframes gradientShift{0%{background-position:0% 50%}50%{background-position:100% 50%}100%{background-position:0% 50%}}
@keyframes float{0%,100%{transform:translateY(0)}50%{transform:translateY(-4px)}}
@keyframes glow{0%,100%{box-shadow:0 0 8px rgba(56,189,248,.4)}50%{box-shadow:0 0 20px rgba(56,189,248,.7)}}
body{background:radial-gradient(900px 500px at 0% 0%,#122340 0%,var(--bg) 55%);
color:var(--txt);min-height:100vh;padding:16px;line-height:1.55}
.wrap{max-width:1140px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);
border-radius:18px;padding:26px;box-shadow:0 24px 80px rgba(0,0,0,.55);
animation:fadeIn .35s ease-out}
h1{background:linear-gradient(90deg,var(--accent),var(--purple),var(--accent));
background-size:200% auto;-webkit-background-clip:text;
-webkit-text-fill-color:transparent;font-size:23px;margin-bottom:5px;
display:flex;gap:9px;align-items:center;letter-spacing:-.3px;
animation:gradientShift 4s ease infinite}
h2{color:var(--accent);font-size:14px;text-transform:uppercase;
letter-spacing:1.2px;margin:24px 0 10px;display:flex;gap:7px;
align-items:center;border-left:3px solid var(--accent);
padding-left:10px;font-weight:700}
h3{font-size:12px;color:var(--muted);margin:12px 0 6px;
text-transform:uppercase;letter-spacing:.8px;font-weight:600}
.muted{color:var(--muted);font-size:13px;margin-bottom:10px}
input,select,textarea,button{font-size:14.5px;padding:11px 13px;
border-radius:10px;border:1px solid #344256;background:var(--card2);
color:var(--txt);width:100%;margin-bottom:10px;outline:none;
transition:.18s;font-family:inherit}
input:focus,select:focus,textarea:focus{border-color:var(--accent);
box-shadow:0 0 0 3px rgba(56,189,248,.13);transform:translateY(-1px)}
button{background:linear-gradient(135deg,var(--accent),var(--accent2));
color:#0a0f1a;font-weight:700;border:none;cursor:pointer;
display:inline-flex;align-items:center;justify-content:center;
gap:6px;transition:.22s;position:relative;overflow:hidden}
button:hover{transform:translateY(-2px);
box-shadow:0 8px 24px rgba(56,189,248,.35)}
button:active{transform:translateY(0) scale(.97)}
button::before{content:"";position:absolute;inset:0;
background:linear-gradient(90deg,transparent,rgba(255,255,255,.2),transparent);
background-size:200% 100%;opacity:0;transition:opacity .2s}
button:hover::before{opacity:1;animation:shimmer 1.2s linear infinite}
.btn-red{background:linear-gradient(135deg,#ef4444,#dc2626);color:#fff}
.btn-red:hover{box-shadow:0 8px 24px rgba(239,68,68,.4)}
.btn-green{background:linear-gradient(135deg,#10b981,#059669);color:#fff}
.btn-green:hover{box-shadow:0 8px 24px rgba(16,185,129,.4)}
.btn-purple{background:linear-gradient(135deg,#a78bfa,#8b5cf6);color:#fff}
.btn-purple:hover{box-shadow:0 8px 24px rgba(167,139,250,.4)}
.btn-ghost{background:transparent;border:1px solid #344256;color:#cbd5e1}
.btn-ghost:hover{background:#1a2740;border-color:var(--accent)}
.btn-sm{padding:6px 11px;font-size:12px;border-radius:7px;width:auto;
margin-bottom:0;display:inline-flex}
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
gap:10px;margin-bottom:6px}
.row>*{margin-bottom:0}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
gap:11px;margin:14px 0}
.stat{background:var(--card2);border:1px solid var(--line);
border-radius:13px;padding:14px 16px;position:relative;
overflow:hidden;animation:fadeIn .4s ease-out both}
.stat:nth-child(1){animation-delay:.05s}
.stat:nth-child(2){animation-delay:.1s}
.stat:nth-child(3){animation-delay:.15s}
.stat:nth-child(4){animation-delay:.2s}
.stat:nth-child(5){animation-delay:.25s}
.stat:nth-child(6){animation-delay:.3s}
.stat:nth-child(7){animation-delay:.35s}
.stat::before{content:"";position:absolute;top:0;left:0;right:0;height:2px;
background:linear-gradient(90deg,transparent,var(--accent),transparent)}
.stat .l{font-size:11px;color:var(--muted);text-transform:uppercase;
letter-spacing:.8px;margin-bottom:6px;font-weight:600}
.stat .v{font-size:25px;font-weight:800;color:var(--accent);
letter-spacing:-.5px;line-height:1.1}
.stat .v.g{color:var(--green)}.stat .v.r{color:var(--red)}
.stat .v.o{color:var(--amber)}.stat .v.p{color:var(--purple)}
.stat .v.small{font-size:13px;font-weight:600;color:#cbd5e1;letter-spacing:0}
table{width:100%;border-collapse:collapse;margin-top:6px;font-size:13.5px;
background:var(--card2);border-radius:11px;overflow:hidden}
th,td{padding:10px 11px;border-bottom:1px solid var(--line);
text-align:left;vertical-align:middle}
th{color:var(--muted);background:#0a1424;font-weight:600;font-size:11px;
text-transform:uppercase;letter-spacing:.7px}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(56,189,248,.05)}
.badge{display:inline-block;padding:2px 9px;border-radius:6px;
background:#1e293b;font-size:11.5px;color:#cbd5e1;font-weight:600;
border:1px solid #334155}
.badge.g{background:rgba(16,185,129,.15);color:#6ee7b7;border-color:rgba(16,185,129,.3)}
.badge.r{background:rgba(239,68,68,.15);color:#fca5a5;border-color:rgba(239,68,68,.3)}
.badge.o{background:rgba(245,158,11,.15);color:#fcd34d;border-color:rgba(245,158,11,.3);
animation:pulse 1.6s ease-in-out infinite}
.badge.b{background:rgba(56,189,248,.15);color:#7dd3fc;border-color:rgba(56,189,248,.3)}
.badge.p{background:rgba(167,139,250,.15);color:#c4b5fd;border-color:rgba(167,139,250,.3)}
.alert{padding:11px 14px;border-radius:10px;margin-bottom:13px;font-size:14px;
display:flex;gap:8px;align-items:center;animation:fadeIn .3s ease-out}
.alert.err{background:rgba(127,29,29,.5);color:#fecaca;border:1px solid #991b1b}
.alert.ok{background:rgba(20,83,45,.5);color:#bbf7d0;border:1px solid #166534}
.alert.info{background:rgba(30,58,138,.4);color:#bfdbfe;border:1px solid #1e40af}
.alert.warn{background:rgba(120,53,15,.4);color:#fed7aa;border:1px solid #92400e}
nav{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:18px;
padding-bottom:14px;border-bottom:1px solid var(--line)}
nav a{color:var(--accent);text-decoration:none;padding:8px 13px;
border-radius:9px;border:1px solid var(--line);font-size:13px;
font-weight:600;transition:.2s;background:var(--card2)}
nav a:hover{background:#1a2740;border-color:var(--accent);
transform:translateY(-2px);box-shadow:0 4px 12px rgba(56,189,248,.2)}
.code{font-family:ui-monospace,'SF Mono',Menlo,monospace;
background:#0a1424;padding:3px 8px;border-radius:6px;
border:1px solid var(--line);font-size:13px;letter-spacing:.5px}
.term{background:#000;color:#4ade80;padding:16px;border-radius:11px;
font-family:ui-monospace,'SF Mono',Menlo,monospace;max-height:520px;
overflow-y:auto;font-size:12px;line-height:1.5;border:1px solid #1f2937;
box-shadow:inset 0 0 40px rgba(34,197,94,.05)}
.term pre{margin:0;white-space:pre-wrap;word-wrap:break-word;color:inherit}
.term::-webkit-scrollbar{width:8px}
.term::-webkit-scrollbar-thumb{background:#334155;border-radius:4px}
.footer{text-align:center;color:#475569;font-size:11.5px;
margin-top:22px;padding:14px}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.form-inline{display:grid;
grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;align-items:end}
.form-inline button{margin-bottom:0}
hr{border:none;border-top:1px solid var(--line);margin:16px 0}
.link-row{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.pulse{animation:pulse 1.8s ease-in-out infinite}
.float{animation:float 3s ease-in-out infinite}
.glow{animation:glow 2s ease-in-out infinite}
</style></head><body><div class="wrap">
<div class="card">{{ content|safe }}</div>
<div class="footer">PyHost · Bot Hosting · Auto-Install Engine · All Files</div>
</div></body></html>"""

def page(html_body, title="PyHost"):
    return render_template_string(BASE, content=html_body, title=title)

# ---------------- USER ROUTES ----------------
@app.route("/")
def index():
    if session.get("user_ok"): return redirect(url_for("dashboard"))
    msg = ""
    e = request.args.get("err")
    if e == "invalid":
        msg = '<div class="alert err">❌ Galat ya expired access code.</div>'
    elif e == "expired":
        msg = '<div class="alert err">⏰ Aapka code expire ho gaya hai.</div>'
    body = f"""
    <h1>🔐 PyHost — Guest Access</h1>
    <p class="muted">Apna access code enter karein — koi signup nahi.</p>
    {msg}
    <form method="post" action="/access">
      <input name="code" placeholder="Access code" autofocus required>
      <button type="submit">Enter →</button>
    </form>
    <div class="alert info" style="margin-top:18px">
      ℹ️ Access code admin se lein. Har code ka apna limit aur expiry hai.
    </div>"""
    return page(body, "Access")

@app.route("/access", methods=["POST"])
def access():
    code = request.form.get("code", "").strip()
    if not code: return redirect(url_for("index", err="invalid"))
    conn = db()
    r = conn.execute("SELECT * FROM access_codes WHERE code=? AND active=1",
                     (code,)).fetchone()
    if not r:
        conn.close(); return redirect(url_for("index", err="invalid"))
    if r["expires_at"] and time.time() > r["expires_at"]:
        conn.close(); return redirect(url_for("index", err="expired"))
    if r["max_uses"] > 0 and r["used"] >= r["max_uses"]:
        conn.close(); return redirect(url_for("index", err="invalid"))
    conn.execute("UPDATE access_codes SET used=used+1 WHERE id=?", (r["id"],))
    conn.execute("INSERT INTO users (code,ip,first_seen,last_seen) VALUES (?,?,?,?)",
                 (code, request.remote_addr, time.time(), time.time()))
    conn.commit(); conn.close()
    session["user_ok"] = True
    session["user_code"] = code
    send_telegram(f"👤 <b>User login</b>\nCode: <code>{code}</code>\nIP: <code>{request.remote_addr}</code>")
    return redirect(url_for("dashboard"))

@app.route("/dashboard")
@user_required
def dashboard():
    ip = request.remote_addr; code = session.get("user_code", "")
    conn = db()
    conn.execute("UPDATE users SET last_seen=? WHERE code=? AND ip=?",
                 (time.time(), code, ip))
    conn.commit()
    files = conn.execute("SELECT * FROM files WHERE owner_ip=? ORDER BY id DESC LIMIT 50",
                         (ip,)).fetchall()
    rows = ""
    for f in files:
        st = "—"
        if f["install_job_id"]:
            j = conn.execute("SELECT status FROM install_jobs WHERE id=?",
                             (f["install_job_id"],)).fetchone()
            if j: st = j["status"]
        badge = {"success": '<span class="badge g">✅ Installed</span>',
                 "failed": '<span class="badge r">❌ Failed</span>',
                 "running": '<span class="badge o">⏳ Installing</span>',
                 "pending": '<span class="badge o">⏳ Queued</span>',
                 "skipped": '<span class="badge">⏭ Hosted</span>'}.get(st, '—')
        rows += (f"<tr><td>{html.escape(f['orig'])}</td>"
                 f"<td>{f['size']//1024} KB</td>"
                 f"<td>{f['uploaded_at'][:16]}</td><td>{badge}</td>"
                 f"<td><a href='/hosted/{f['filename']}' target='_blank'>Open</a></td></tr>")
    if not rows: rows = '<tr><td colspan=5 class="muted">Abhi koi file nahi.</td></tr>'
    conn.close()
    body = f"""
    <nav>
      <a href="/dashboard">📤 Dashboard</a>
      <a href="/my-logs">📜 My Logs</a>
      <a href="/logout">🚪 Logout</a>
    </nav>
    <h1>📤 Aapka Dashboard</h1>
    <p class="muted">Code: <span class="code">{code}</span> · IP: {ip}</p>
    <h2>Upload File</h2>
    <form method="post" action="/upload" enctype="multipart/form-data">
      <input type="file" name="file" required>
      <button class="btn-green" type="submit">⬆️ Upload & Auto-Install</button>
    </form>
    <div class="alert info">
      💡 <b>Koi bhi file</b> — ZIP, PY, JSON, image, video, APK, EXE… sab host hongi.
      ZIP mein <code>requirements.txt</code> hoga to auto-install hoga.
    </div>
    <h2>Aapki Files</h2>
    <table><tr><th>Name</th><th>Size</th><th>Time</th><th>Install</th><th>Link</th></tr>{rows}</table>"""
    return page(body, "Dashboard")

@app.route("/my-logs")
@user_required
def my_logs():
    ip = request.remote_addr
    conn = db()
    files = conn.execute("SELECT * FROM files WHERE owner_ip=? ORDER BY id DESC LIMIT 30",
                         (ip,)).fetchall()
    jobs_data = []
    for f in files:
        if f["install_job_id"]:
            j = conn.execute("SELECT * FROM install_jobs WHERE id=?",
                             (f["install_job_id"],)).fetchone()
            if j: jobs_data.append({"f": f, "j": j})
    conn.close()
    rows = ""
    for d in jobs_data:
        j = d["j"]; f = d["f"]
        st = {"success": '<span class="badge g">✅ Success</span>',
              "failed": '<span class="badge r">❌ Failed</span>',
              "running": '<span class="badge o">⏳ Running</span>',
              "pending": '<span class="badge o">⏳ Pending</span>',
              "skipped": '<span class="badge">⏭ Skipped</span>'}.get(j["status"], j["status"])
        rows += (f"<tr><td>#{j['id']}</td><td>{html.escape(f['orig'])}</td>"
                 f"<td>{j['kind']}</td><td>{st}</td>"
                 f"<td>{j['started_at'][:16]}</td>"
                 f"<td><a class='btn-sm btn-ghost' href='/my-log/{j['id']}'>📜 View</a></td></tr>")
    if not rows: rows = '<tr><td colspan=6 class="muted">Abhi koi install log nahi.</td></tr>'
    body = f"""
    <nav><a href="/dashboard">📤 Dashboard</a><a href="/logout">🚪 Logout</a></nav>
    <h1>📜 Aapke Install Logs</h1>
    <p class="muted">Har upload ka install process log yahan dekho.</p>
    <table><tr><th>#</th><th>File</th><th>Type</th><th>Status</th><th>Started</th><th>Log</th></tr>{rows}</table>"""
    return page(body, "My Logs")

@app.route("/my-log/<int:jid>")
@user_required
def my_log_view(jid):
    conn = db()
    j = conn.execute("SELECT * FROM install_jobs WHERE id=?", (jid,)).fetchone()
    if j and j["file_id"]:
        f = conn.execute("SELECT * FROM files WHERE id=?", (j["file_id"],)).fetchone()
        if f and f["owner_ip"] != request.remote_addr:
            conn.close(); abort(403)
    conn.close()
    if not j: abort(404)
    live = j["status"] in ("running", "pending")
    body = f"""
    <nav><a href="/my-logs">← Back</a></nav>
    <h1>📜 Log #{jid}</h1>
    <p class="muted">Kind: <span class="badge b">{j['kind']}</span> · Status: {j['status']}</p>
    <div class="term"><pre>{html.escape(j['log'] or '')}</pre></div>
    {'<script>setTimeout(()=>location.reload(),2200)</script>' if live else ''}"""
    return page(body, f"Log #{jid}")

@app.route("/upload", methods=["POST"])
@user_required
def upload():
    f = request.files.get("file")
    if not f or not f.filename: return redirect(url_for("dashboard"))
    safe = secure_filename(f.filename)
    stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    fname = f"{stamp}_{safe}"
    path = os.path.join(UPLOAD_DIR, fname)
    f.save(path)
    size = os.path.getsize(path)
    conn = db()
    cur = conn.execute("""INSERT INTO files
        (filename,orig,owner_code,owner_ip,size,uploaded_at,source)
        VALUES (?,?,?,?,?,?,?)""",
        (fname, f.filename, session.get("user_code", ""),
         request.remote_addr, size, datetime.datetime.now().isoformat(), "web"))
    file_id = cur.lastrowid
    conn.commit(); conn.close()
    send_telegram(f"📤 <b>Upload</b>\n<code>{html.escape(f.filename)}</code>\n{size//1024} KB\nIP: <code>{request.remote_addr}</code>")
    threading.Thread(target=process_uploaded_file,
                     args=(file_id, path, f.filename), daemon=True).start()
    return redirect(url_for("dashboard"))

@app.route("/hosted/<path:name>")
def hosted(name):
    if site_expired(): abort(410)
    return send_from_directory(UPLOAD_DIR, name, as_attachment=False)

@app.route("/logout")
def logout():
    session.clear(); return redirect("/")

@app.route("/expired")
def expired_page():
    return page("""<h1>⏰ Site Expired</h1>
    <p class="muted">Admin-set expiry time ke baad site band ho gayi.</p>""", "Expired")

@app.route("/healthz")
def healthz(): return "ok", 200

# ---------------- ADMIN ----------------
@app.route(ADMIN_PATH + "/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("code") == cfg("admin_code"):
            session["admin"] = True
            send_telegram(f"🛡️ <b>Admin login</b>\nIP: <code>{request.remote_addr}</code>")
            return redirect(ADMIN_PATH)
        return page("""<h1>🔒 Admin</h1>
            <div class="alert err">Galat admin code.</div>
            <form method="post"><input name="code" type="password" autofocus>
            <button>Login</button></form>""", "Admin")
    return page("""<h1>🔒 Admin Login</h1>
        <form method="post">
          <input name="code" type="password" placeholder="Admin code" autofocus required>
          <button>Login</button>
        </form>""", "Admin")

@app.route(ADMIN_PATH + "/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(ADMIN_PATH + "/login")

@app.route(ADMIN_PATH)
@admin_required
def admin_panel():
    now = time.time()
    conn = db()
    active = conn.execute("SELECT COUNT(*) c FROM users WHERE last_seen>?", (now-300,)).fetchone()["c"]
    total  = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    files_c= conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"]
    codes  = conn.execute("SELECT COUNT(*) c FROM access_codes WHERE active=1").fetchone()["c"]
    running= conn.execute("SELECT COUNT(*) c FROM install_jobs WHERE status IN ('running','pending')").fetchone()["c"]
    bots_n = conn.execute("SELECT COUNT(*) c FROM bots WHERE active=1").fetchone()["c"]

    jobs = conn.execute("SELECT * FROM install_jobs ORDER BY id DESC LIMIT 10").fetchall()
    job_rows = ""
    for j in jobs:
        st = {"success":"<span class='badge g'>✅ Success</span>",
              "failed":"<span class='badge r'>❌ Failed</span>",
              "running":"<span class='badge o'>⏳ Running</span>",
              "pending":"<span class='badge o'>⏳ Pending</span>",
              "skipped":"<span class='badge'>⏭ Skipped</span>"}.get(j["status"], j["status"])
        job_rows += (f"<tr><td>#{j['id']}</td><td>{j['kind']}</td><td>{st}</td>"
                     f"<td>{j['started_at'][:16]}</td>"
                     f"<td><a class='btn-sm btn-ghost' href='{ADMIN_PATH}/job/{j['id']}'>📜 Log</a></td></tr>")
    if not job_rows: job_rows = '<tr><td colspan=5 class="muted">No jobs yet.</td></tr>'

    codes_rows = ""
    for c in conn.execute("SELECT * FROM access_codes ORDER BY id DESC LIMIT 30").fetchall():
        exp = (datetime.datetime.fromtimestamp(c["expires_at"]).strftime("%d-%m %H:%M")
               if c["expires_at"] else "∞")
        codes_rows += (f"<tr><td><span class='code'>{c['code']}</span></td>"
                      f"<td>{c['used']}/{c['max_uses'] if c['max_uses'] else '∞'}</td>"
                      f"<td>{exp}</td>"
                      f"<td>{'<span class=badge g>✅ Active</span>' if c['active'] else '<span class=badge r>⛔ Revoked</span>'}</td>"
                      f"<td>{'<a class=btn-sm btn-ghost href='+ADMIN_PATH+'/revoke/'+str(c['id'])+' style=color:#ef4444>Revoke</a>' if c['active'] else ''}</td></tr>")
    if not codes_rows: codes_rows = '<tr><td colspan=5 class="muted">No codes — generate one.</td></tr>'

    users_rows = ""
    for u in conn.execute("SELECT * FROM users ORDER BY last_seen DESC LIMIT 30").fetchall():
        st = "🟢 Online" if now-u["last_seen"]<300 else "⚪ Offline"
        users_rows += (f"<tr><td><span class='code'>{u['code']}</span></td>"
                      f"<td><a href='{ADMIN_PATH}/user/{u['ip']}'>{u['ip']}</a></td>"
                      f"<td>{st}</td>"
                      f"<td>{datetime.datetime.fromtimestamp(u['last_seen']).strftime('%d-%m %H:%M')}</td>"
                      f"<td><a class='btn-sm btn-ghost' href='{ADMIN_PATH}/user/{u['ip']}'>👁 View</a></td></tr>")
    if not users_rows: users_rows = '<tr><td colspan=5 class="muted">No users yet.</td></tr>'

    files_rows = ""
    for f in conn.execute("SELECT * FROM files ORDER BY id DESC LIMIT 30").fetchall():
        src_badge = f"<span class='badge p'>🤖 bot</span>" if f["source"]=="bot" else f"<span class='badge b'>🌐 web</span>"
        files_rows += (f"<tr><td>{f['id']}</td><td>{html.escape(f['orig'][:35])}</td>"
                      f"<td>{src_badge}</td><td>{f['size']//1024} KB</td>"
                      f"<td><a href='{ADMIN_PATH}/delete/{f['id']}' onclick=\"return confirm('Delete?')\" style='color:#ef4444'>🗑</a></td></tr>")
    if not files_rows: files_rows = '<tr><td colspan=5 class="muted">No files.</td></tr>'
    conn.close()

    exp = int(cfg("site_expiry", "0"))
    exp_str = datetime.datetime.fromtimestamp(exp).strftime("%d-%m-%Y %H:%M") if exp else "♾️ Never"

    body = f"""
    <nav>
      <a href="{ADMIN_PATH}">🔄 Refresh</a>
      <a href="{ADMIN_PATH}/bots">🤖 Bots</a>
      <a href="{ADMIN_PATH}/settings">⚙️ Settings</a>
      <a href="{ADMIN_PATH}/telegram">📱 Telegram</a>
      <a href="{ADMIN_PATH}/logout">🚪 Logout</a>
    </nav>
    <h1>🛡️ Admin Control Panel</h1>
    <p class="muted">All-in-one: web uploads + bot uploads + auto-install</p>

    <div class="stats">
      <div class="stat"><div class="l">Active (5m)</div><div class="v g">{active}</div></div>
      <div class="stat"><div class="l">Total Users</div><div class="v">{total}</div></div>
      <div class="stat"><div class="l">Files</div><div class="v">{files_c}</div></div>
      <div class="stat"><div class="l">Active Codes</div><div class="v o">{codes}</div></div>
      <div class="stat"><div class="l">Installing</div><div class="v o">{running}</div></div>
      <div class="stat"><div class="l">Bots</div><div class="v p">{bots_n}</div></div>
      <div class="stat"><div class="l">Site Expiry</div><div class="v small">{exp_str}</div></div>
    </div>

    <h2>🔑 Generate Access Code</h2>
    <form method="post" action="{ADMIN_PATH}/generate" class="form-inline">
      <input name="max_uses" type="number" placeholder="Max uses (0=∞)" value="{cfg('default_max_uses')}">
      <input name="hours" type="number" placeholder="Valid hours (0=∞)" value="{cfg('default_valid_hours')}">
      <button type="submit">+ Generate</button>
    </form>
    <table><tr><th>Code</th><th>Uses</th><th>Expiry</th><th>Status</th><th></th></tr>{codes_rows}</table>

    <h2>📦 Recent Install Jobs</h2>
    <table><tr><th>#</th><th>Kind</th><th>Status</th><th>Started</th><th>Log</th></tr>{job_rows}</table>

    <h2>👥 Users (Click IP to View Details)</h2>
    <table><tr><th>Code</th><th>IP</th><th>Status</th><th>Last Seen</th><th>Action</th></tr>{users_rows}</table>

    <h2>📁 Recent Files</h2>
    <table><tr><th>ID</th><th>Name</th><th>Source</th><th>Size</th><th>Del</th></tr>{files_rows}</table>

    <h2>📝 Manual Records</h2>
    <form method="post" action="{ADMIN_PATH}/add_detail" class="form-inline">
      <input name="name" placeholder="Name">
      <input name="email" placeholder="Email">
      <input name="note" placeholder="Note">
      <button type="submit">+ Add</button>
    </form>
    """
    return page(body, "Admin Panel")

@app.route(ADMIN_PATH + "/user/<path:ip>")
@admin_required
def admin_user_detail(ip):
    now = time.time()
    conn = db()
    users = conn.execute("SELECT * FROM users WHERE ip=? ORDER BY last_seen DESC", (ip,)).fetchall()
    files = conn.execute("SELECT * FROM files WHERE owner_ip=? ORDER BY id DESC", (ip,)).fetchall()
    file_rows = ""
    for f in files:
        st = "—"
        if f["install_job_id"]:
            j = conn.execute("SELECT id,status FROM install_jobs WHERE id=?",
                             (f["install_job_id"],)).fetchone()
            if j: st = j["status"]
        badge = {"success":'<span class="badge g">✅ Installed</span>',
                 "failed":'<span class="badge r">❌ Failed</span>',
                 "running":'<span class="badge o">⏳ Installing</span>',
                 "pending":'<span class="badge o">⏳ Queued</span>',
                 "skipped":'<span class="badge">⏭ Hosted</span>'}.get(st, '—')
        src_badge = f"<span class='badge p'>🤖 bot</span>" if f["source"]=="bot" else f"<span class='badge b'>🌐 web</span>"
        file_rows += (f"<tr><td>{f['id']}</td><td>{html.escape(f['orig'])}</td>"
                     f"<td>{src_badge}</td><td>{f['size']//1024} KB</td>"
                     f"<td>{f['uploaded_at'][:16]}</td><td>{badge}</td>"
                     f"<td>{'<a class=btn-sm btn-ghost href='+ADMIN_PATH+'/job/'+str(f['install_job_id'])+'>📜 Log</a>' if f['install_job_id'] else ''}</td></tr>")
    if not file_rows: file_rows = '<tr><td colspan=7 class="muted">No files</td></tr>'
    user_rows = ""
    for u in users:
        st = "🟢 Online" if now-u["last_seen"]<300 else "⚪ Offline"
        user_rows += (f"<tr><td><span class='code'>{u['code']}</span></td>"
                     f"<td>{st}</td>"
                     f"<td>{datetime.datetime.fromtimestamp(u['first_seen']).strftime('%d-%m %H:%M')}</td>"
                     f"<td>{datetime.datetime.fromtimestamp(u['last_seen']).strftime('%d-%m %H:%M')}</td></tr>")
    conn.close()
    body = f"""
    <nav><a href="{ADMIN_PATH}">← Back</a></nav>
    <h1>👤 User Detail — <span class="code">{ip}</span></h1>
    <h2>Sessions</h2>
    <table><tr><th>Code</th><th>Status</th><th>First Seen</th><th>Last Seen</th></tr>{user_rows or '<tr><td colspan=4 class=muted>No sessions</td></tr>'}</table>
    <h2>Files Uploaded ({len(files)})</h2>
    <table><tr><th>ID</th><th>Name</th><th>Source</th><th>Size</th><th>Time</th><th>Install</th><th>Log</th></tr>{file_rows}</table>
    """
    return page(body, "User Detail")

@app.route(ADMIN_PATH + "/job/<int:jid>")
@admin_required
def view_job(jid):
    conn = db()
    j = conn.execute("SELECT * FROM install_jobs WHERE id=?", (jid,)).fetchone()
    conn.close()
    if not j: abort(404)
    live = j["status"] in ("running", "pending")
    body = f"""
    <nav><a href="{ADMIN_PATH}">← Back</a></nav>
    <h1>📜 Job #{jid}</h1>
    <p class="muted">Kind: <span class="badge b">{j['kind']}</span> · Status: {j['status']} · Started: {j['started_at'][:19]}</p>
    <div class="term"><pre>{html.escape(j['log'] or '')}</pre></div>
    {'<script>setTimeout(()=>location.reload(),2200)</script>' if live else ''}"""
    return page(body, f"Job #{jid}")

@app.route(ADMIN_PATH + "/generate", methods=["POST"])
@admin_required
def generate_code():
    mu = int(request.form.get("max_uses", 10) or 10)
    hrs = float(request.form.get("hours", 24) or 24)
    code = str(secrets.randbelow(9000) + 1000)
    expires = (time.time() + hrs*3600) if hrs > 0 else None
    conn = db()
    conn.execute("""INSERT INTO access_codes
        (code,max_uses,used,expires_at,active,created_at)
        VALUES (?,?,0,?,1,?)""",
        (code, mu, expires, datetime.datetime.now().isoformat()))
    conn.commit(); conn.close()
    send_telegram(f"🎟️ <b>New code</b>\n<code>{code}</code>\nUses: {mu if mu else '∞'} · Valid: {hrs if hrs else '∞'}h")
    return redirect(ADMIN_PATH)

@app.route(ADMIN_PATH + "/revoke/<int:cid>")
@admin_required
def revoke_code(cid):
    conn = db()
    conn.execute("UPDATE access_codes SET active=0 WHERE id=?", (cid,))
    conn.commit(); conn.close()
    return redirect(ADMIN_PATH)

@app.route(ADMIN_PATH + "/delete/<int:fid>")
@admin_required
def delete_file(fid):
    conn = db()
    f = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    if f:
        try: os.remove(os.path.join(UPLOAD_DIR, f["filename"]))
        except: pass
        if f["extract_path"]:
            try: shutil.rmtree(f["extract_path"])
            except: pass
        conn.execute("DELETE FROM files WHERE id=?", (fid,))
        conn.commit()
    conn.close()
    return redirect(ADMIN_PATH)

@app.route(ADMIN_PATH + "/add_detail", methods=["POST"])
@admin_required
def add_detail():
    conn = db()
    conn.execute("INSERT INTO user_details (name,email,note,created_at) VALUES (?,?,?,?)",
                 (request.form.get("name",""), request.form.get("email",""),
                  request.form.get("note",""), datetime.datetime.now().isoformat()))
    conn.commit(); conn.close()
    return redirect(ADMIN_PATH)

@app.route(ADMIN_PATH + "/settings", methods=["GET", "POST"])
@admin_required
def settings():
    if request.method == "POST":
        act = request.form.get("action")
        if act == "expiry":
            try:
                dt = datetime.datetime.strptime(request.form.get("expiry",""), "%Y-%m-%dT%H:%M")
                set_cfg("site_expiry", int(dt.timestamp()))
            except: pass
        elif act == "clear_expiry":
            set_cfg("site_expiry", "0")
        elif act == "admin_code":
            nc = request.form.get("code","").strip()
            if nc: set_cfg("admin_code", nc)
        elif act == "defaults":
            set_cfg("default_max_uses", request.form.get("mu", 10))
            set_cfg("default_valid_hours", request.form.get("hrs", 24))
        elif act == "auto_install":
            set_cfg("auto_install_on_startup", "1" if request.form.get("on")=="1" else "0")
        return redirect(ADMIN_PATH + "/settings")

    exp = int(cfg("site_expiry", "0"))
    exp_val = datetime.datetime.fromtimestamp(exp).strftime("%Y-%m-%dT%H:%M") if exp else ""
    body = f"""
    <nav><a href="{ADMIN_PATH}">← Back</a></nav>
    <h1>⚙️ Settings</h1>
    <h2>⏰ Site Auto-Expiry</h2>
    <form method="post">
      <input type="hidden" name="action" value="expiry">
      <input type="datetime-local" name="expiry" value="{exp_val}">
      <button class="btn-red">⏰ Set Expiry</button>
    </form>
    <form method="post" style="margin-top:6px">
      <input type="hidden" name="action" value="clear_expiry">
      <button class="btn-ghost">♾️ Never Expire</button>
    </form>
    <h2>🎟️ Defaults</h2>
    <form method="post" class="form-inline">
      <input type="hidden" name="action" value="defaults">
      <input name="mu" type="number" placeholder="Max uses" value="{cfg('default_max_uses')}">
      <input name="hrs" type="number" placeholder="Hours" value="{cfg('default_valid_hours')}">
      <button>Save</button>
    </form>
    <h2>📦 Auto-Install on Startup</h2>
    <form method="post" class="form-inline">
      <input type="hidden" name="action" value="auto_install">
      <select name="on">
        <option value="1" {'selected' if cfg('auto_install_on_startup','1')=='1' else ''}>🟢 Enabled</option>
        <option value="0" {'selected' if cfg('auto_install_on_startup','1')=='0' else ''}>⚪ Disabled</option>
      </select>
      <button>Save</button>
    </form>
    <h2>🛡️ Change Admin Code</h2>
    <form method="post">
      <input type="hidden" name="action" value="admin_code">
      <input name="code" placeholder="New admin code">
      <button>Update</button>
    </form>"""
    return page(body, "Settings")

@app.route(ADMIN_PATH + "/telegram", methods=["GET", "POST"])
@admin_required
def telegram_settings():
    if request.method == "POST":
        act = request.form.get("action")
        if act == "save":
            set_cfg("telegram_token", request.form.get("token","").strip())
            set_cfg("telegram_chat_id", request.form.get("chat","").strip())
            set_cfg("telegram_enabled", "1" if request.form.get("enabled")=="1" else "0")
        elif act == "test":
            send_telegram("🧪 <b>PyHost Test</b>\nTelegram connected!")
        return redirect(ADMIN_PATH + "/telegram")
    enabled = cfg("telegram_enabled","0") == "1"
    body = f"""
    <nav><a href="{ADMIN_PATH}">← Back</a></nav>
    <h1>📱 Telegram Notifications</h1>
    <p class="muted">Admin ko notifications: uploads, installs, logins.</p>
    <div class="alert info">
      1️⃣ @BotFather → /newbot → token copy<br>
      2️⃣ Bot ko /start bhejein<br>
      3️⃣ @userinfobot → chat ID<br>
      4️⃣ Neeche save karein
    </div>
    <form method="post">
      <input type="hidden" name="action" value="save">
      <h3>Bot Token</h3>
      <input name="token" placeholder="123:ABC..." value="{cfg('telegram_token')}">
      <h3>Chat ID</h3>
      <input name="chat" placeholder="123456789" value="{cfg('telegram_chat_id')}">
      <h3>Status</h3>
      <select name="enabled">
        <option value="1" {'selected' if enabled else ''}>🟢 Enabled</option>
        <option value="0" {'selected' if not enabled else ''}>⚪ Disabled</option>
      </select>
      <button>💾 Save</button>
    </form>
    <form method="post" style="margin-top:8px">
      <input type="hidden" name="action" value="test">
      <button class="btn-ghost">🧪 Test Message</button>
    </form>"""
    return page(body, "Telegram")

# ---------------- BOT HOSTING ----------------
def bot_api(token, method, **params):
    try:
        import requests
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}",
                          json=params, timeout=15)
        return r.json()
    except Exception as e:
        return {"ok": False, "description": str(e)}

def bot_send(token, chat_id, text, **extra):
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    params.update(extra)
    return bot_api(token, "sendMessage", **params)

def handle_bot_message(bot, update):
    msg = update.get("message")
    if not msg: return
    chat_id = str(msg["chat"]["id"])
    text = (msg.get("text") or "").strip()
    user_name = msg.get("from", {}).get("first_name", "User")
    site = (bot["webhook_url"] or "").split("/bot-hook/")[0]

    if text == "/start":
        bot_send(bot["token"], chat_id,
            f"👋 Hello <b>{html.escape(user_name)}</b>!\n\n"
            "I'm a <b>hosting bot</b>. Send me any file to host it.\n\n"
            "📤 <b>Supported:</b> ZIP, PY, JSON, images, video, APK, EXE — anything!\n"
            "🤖 ZIP with requirements.txt → auto-install\n\n"
            "<b>Commands:</b>\n"
            "/files — your hosted files\n"
            "/stats — stats\n"
            "/id — your chat ID\n"
            "/help — help")
        return

    if text == "/help":
        bot_send(bot["token"], chat_id,
            "📖 <b>How to use</b>\n\n"
            "1. Send any file as document\n"
            "2. I download & host it\n"
            "3. Get direct link\n\n"
            "Limit: 100 MB\n"
            "ZIP: auto-extract + install requirements.txt")
        return

    if text == "/id":
        bot_send(bot["token"], chat_id, f"🆔 Chat ID: <code>{chat_id}</code>")
        return

    if text == "/stats":
        conn = db()
        n = conn.execute("SELECT COUNT(*) c FROM files WHERE source='bot'").fetchone()["c"]
        conn.close()
        bot_send(bot["token"], chat_id, f"📊 Bot files hosted: <b>{n}</b>")
        return

    if text == "/files":
        conn = db()
        files = conn.execute(
            "SELECT * FROM files WHERE source='bot' AND owner_ip=? ORDER BY id DESC LIMIT 15",
            (chat_id,)).fetchall()
        conn.close()
        if not files:
            bot_send(bot["token"], chat_id, "📂 Nothing uploaded yet.")
            return
        lines = [f"📂 <b>Your Files ({len(files)})</b>\n"]
        for f in files:
            lines.append(f"• <a href='{site}/hosted/{f['filename']}'>{html.escape(f['orig'][:40])}</a> ({f['size']//1024} KB)")
        bot_send(bot["token"], chat_id, "\n".join(lines), disable_web_page_preview=True)
        return

    # File uploads
    file_id = None; filename = None
    if msg.get("document"):
        file_id = msg["document"]["file_id"]
        filename = msg["document"].get("file_name") or f"file_{int(time.time())}"
    elif msg.get("photo"):
        file_id = msg["photo"][-1]["file_id"]
        filename = f"photo_{int(time.time())}.jpg"
    elif msg.get("audio"):
        file_id = msg["audio"]["file_id"]
        filename = msg["audio"].get("file_name") or f"audio_{int(time.time())}.mp3"
    elif msg.get("video"):
        file_id = msg["video"]["file_id"]
        filename = msg["video"].get("file_name") or f"video_{int(time.time())}.mp4"
    elif msg.get("voice"):
        file_id = msg["voice"]["file_id"]
        filename = f"voice_{int(time.time())}.ogg"
    elif msg.get("video_note"):
        file_id = msg["video_note"]["file_id"]
        filename = f"vnote_{int(time.time())}.mp4"

    if file_id:
        _process_bot_file(bot, chat_id, file_id, filename, site)
    elif text:
        bot_send(bot["token"], chat_id,
                 f"ℹ️ Send a file to host. /help for commands.")

def _process_bot_file(bot, chat_id, file_id, filename, site):
    bot_send(bot["token"], chat_id, f"⏳ Downloading <b>{html.escape(filename)}</b>…")
    try:
        info = bot_api(bot["token"], "getFile", file_id=file_id)
        if not info.get("ok"):
            bot_send(bot["token"], chat_id, "❌ Cannot get file")
            return
        fpath = info["result"]["file_path"]
        url = f"https://api.telegram.org/file/bot{bot['token']}/{fpath}"
        import requests
        r = requests.get(url, timeout=180, stream=True)
        if r.status_code != 200:
            bot_send(bot["token"], chat_id, f"❌ Download failed ({r.status_code})")
            return
        safe = secure_filename(filename) or f"file_{int(time.time())}"
        stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        fname = f"bot_{stamp}_{safe}"
        path = os.path.join(UPLOAD_DIR, fname)
        size = 0
        with open(path, "wb") as fh:
            for chunk in r.iter_content(65536):
                fh.write(chunk); size += len(chunk)
        conn = db()
        cur = conn.execute("""INSERT INTO files
            (filename,orig,owner_code,owner_ip,size,uploaded_at,source)
            VALUES (?,?,?,?,?,?,?)""",
            (fname, filename, f"bot:{bot['id']}", chat_id, size,
             datetime.datetime.now().isoformat(), "bot"))
        file_db_id = cur.lastrowid
        conn.commit(); conn.close()
        threading.Thread(target=process_uploaded_file,
                         args=(file_db_id, path, filename), daemon=True).start()
        link = f"{site}/hosted/{fname}"
        bot_send(bot["token"], chat_id,
            f"✅ <b>Uploaded!</b>\n\n"
            f"📁 <code>{html.escape(filename)}</code>\n"
            f"📦 {size//1024} KB\n\n"
            f"🔗 <a href='{link}'>Download Link</a>",
            disable_web_page_preview=True)
        if bot["admin_chat"] and bot["admin_chat"] != chat_id:
            bot_send(bot["token"], bot["admin_chat"],
                f"📤 <b>Bot upload</b>\n"
                f"From: <code>{chat_id}</code>\n"
                f"File: {html.escape(filename)}\n"
                f"Size: {size//1024} KB")
        send_telegram(f"📤 <b>Bot upload</b>\nFrom: <code>{chat_id}</code>\n{html.escape(filename)}")
    except Exception as e:
        try: bot_send(bot["token"], chat_id, f"❌ Error: {html.escape(str(e)[:200])}")
        except: pass

@app.route("/bot-hook/<secret>", methods=["POST"])
def bot_hook(secret):
    conn = db()
    bot = conn.execute("SELECT * FROM bots WHERE secret=? AND active=1", (secret,)).fetchone()
    conn.close()
    if not bot:
        return "no bot", 404
    try:
        upd = request.get_json(force=True, silent=True) or {}
        threading.Thread(target=handle_bot_message, args=(dict(bot), upd), daemon=True).start()
    except Exception as e:
        print(f"bot hook error: {e}")
    return "ok"

@app.route(ADMIN_PATH + "/bots", methods=["GET"])
@admin_required
def admin_bots():
    conn = db()
    bots = conn.execute("SELECT * FROM bots ORDER BY id DESC").fetchall()
    conn.close()
    msg = ""
    if request.args.get("ok"): msg = '<div class="alert ok">✅ Bot activated!</div>'
    if request.args.get("err"): msg = f'<div class="alert err">❌ Error: {html.escape(request.args["err"])}</div>'

    rows = ""
    for b in bots:
        st = '<span class="badge g">🟢 Active</span>' if b["active"] else '<span class="badge">⚪ Inactive</span>'
        actions = ""
        if not b["active"]:
            actions = f"<a class='btn-sm btn-green' href='{ADMIN_PATH}/bots/activate/{b['id']}'>▶️ Activate</a>"
        else:
            actions = f"<a class='btn-sm btn-ghost' href='{ADMIN_PATH}/bots/deactivate/{b['id']}'>⏹ Deactivate</a>"
        actions += f" <a class='btn-sm btn-ghost' href='{ADMIN_PATH}/bots/delete/{b['id']}' onclick=\"return confirm('Delete?')\" style='color:#ef4444'>🗑</a>"
        token_mask = (b["token"][:8] + "…" + b["token"][-4:]) if len(b["token"]) > 12 else "***"
        rows += (f"<tr><td>{b['id']}</td><td><code>{token_mask}</code></td>"
                f"<td>{b['admin_chat'] or '—'}</td><td>{b['bot_username'] or '—'}</td>"
                f"<td>{st}</td><td>{actions}</td></tr>")
    if not rows: rows = '<tr><td colspan=6 class="muted">No bots yet.</td></tr>'

    body = f"""
    <nav><a href="{ADMIN_PATH}">← Back</a></nav>
    <h1>🤖 Bot Hosting</h1>
    <p class="muted">Ek Telegram bot banaiye — woh bhi files host karega (ZIP, PY, etc.)</p>
    {msg}
    <div class="alert info">
      <b>Setup:</b><br>
      1️⃣ Telegram → @BotFather → /newbot → token copy<br>
      2️⃣ Apni chat ID @userinfobot se lein<br>
      3️⃣ Neeche add karein → <b>Activate</b> dabayein<br>
      4️⃣ Bot ko /start bhejein → ab bot se bhi files upload hongi!
    </div>
    <h2>Add New Bot</h2>
    <form method="post" action="{ADMIN_PATH}/bots/add">
      <h3>Bot Token</h3>
      <input name="token" placeholder="1234567890:ABC-DEF..." required>
      <h3>Admin Chat ID</h3>
      <input name="admin_chat" placeholder="Your Telegram chat ID (optional notifications)" value="">
      <button class="btn-purple" type="submit">+ Add Bot</button>
    </form>

    <h2>Your Bots ({len(bots)})</h2>
    <table>
      <tr><th>#</th><th>Token</th><th>Admin Chat</th><th>Bot Username</th><th>Status</th><th>Actions</th></tr>
      {rows}
    </table>
    """
    return page(body, "Bot Hosting")

@app.route(ADMIN_PATH + "/bots/add", methods=["POST"])
@admin_required
def add_bot():
    token = request.form.get("token","").strip()
    chat  = request.form.get("admin_chat","").strip()
    if not token: return redirect(ADMIN_PATH + "/bots")
    me = bot_api(token, "getMe")
    username = me.get("result", {}).get("username", "") if me.get("ok") else ""
    secret = secrets.token_urlsafe(24)
    conn = db()
    conn.execute("INSERT INTO bots (token,admin_chat,secret,active,created_at,bot_username) "
                 "VALUES (?,?,?,0,?,?)",
                 (token, chat, secret, datetime.datetime.now().isoformat(), username))
    conn.commit(); conn.close()
    return redirect(ADMIN_PATH + "/bots")

@app.route(ADMIN_PATH + "/bots/activate/<int:bid>")
@admin_required
def activate_bot(bid):
    conn = db()
    bot = conn.execute("SELECT * FROM bots WHERE id=?", (bid,)).fetchone()
    conn.close()
    if not bot: return redirect(ADMIN_PATH + "/bots")
    base = SITE_URL_ENV or request.url_root.rstrip("/")
    webhook = f"{base}/bot-hook/{bot['secret']}"
    r = bot_api(bot["token"], "setWebhook", url=webhook, allowed_updates=["message"])
    if r.get("ok"):
        conn = db()
        conn.execute("UPDATE bots SET active=1, webhook_url=? WHERE id=?", (webhook, bid))
        conn.commit(); conn.close()
        return redirect(ADMIN_PATH + "/bots?ok=1")
    return redirect(ADMIN_PATH + "/bots?err=" + html.escape(r.get("description","unknown")))

@app.route(ADMIN_PATH + "/bots/deactivate/<int:bid>")
@admin_required
def deactivate_bot(bid):
    conn = db()
    bot = conn.execute("SELECT * FROM bots WHERE id=?", (bid,)).fetchone()
    conn.close()
    if bot:
        bot_api(bot["token"], "deleteWebhook")
    conn = db()
    conn.execute("UPDATE bots SET active=0 WHERE id=?", (bid,))
    conn.commit(); conn.close()
    return redirect(ADMIN_PATH + "/bots")

@app.route(ADMIN_PATH + "/bots/delete/<int:bid>")
@admin_required
def delete_bot(bid):
    conn = db()
    bot = conn.execute("SELECT * FROM bots WHERE id=?", (bid,)).fetchone()
    conn.close()
    if bot:
        try: bot_api(bot["token"], "deleteWebhook")
        except: pass
    conn = db()
    conn.execute("DELETE FROM bots WHERE id=?", (bid,))
    conn.commit(); conn.close()
    return redirect(ADMIN_PATH + "/bots")

# ---------------- RUN ----------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
