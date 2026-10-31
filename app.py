import os, time, datetime, sqlite3, secrets, shutil
from flask import (Flask, request, render_template_string, session,
                   redirect, url_for, send_from_directory, abort)
from functools import wraps
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

# ---------- PATHS (Netrun /data = permanent) ----------
DATA_DIR    = os.environ.get("DATA_DIR", "/data")
if not os.path.isdir(DATA_DIR):
    DATA_DIR = os.path.abspath("local_data")   # local testing fallback
UPLOAD_DIR  = os.path.join(DATA_DIR, "uploads")
DB_PATH     = os.path.join(DATA_DIR, "site.db")
os.makedirs(UPLOAD_DIR, exist_ok=True)

MAX_UPLOAD_MB = 45
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

# ---------- HIDDEN CONFIG ----------
ADMIN_PATH          = "/x-admin-7k9p2"   # 🔒 change if you want
DEFAULT_ADMIN_CODE  = "1298"
DEFAULT_ACCESS_CODE = "1234"

# ---------- DATABASE ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db(); c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT, ip TEXT, first_seen REAL, last_seen REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS access_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE, max_uses INTEGER, used INTEGER DEFAULT 0,
        expires_at REAL, active INTEGER DEFAULT 1,
        created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT, orig TEXT, owner_code TEXT, owner_ip TEXT,
        size INTEGER, uploaded_at TEXT, is_public INTEGER DEFAULT 1)""")
    c.execute("""CREATE TABLE IF NOT EXISTS user_details (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, email TEXT, note TEXT, created_at TEXT)""")
    defaults = {
        "admin_code":  DEFAULT_ADMIN_CODE,
        "site_expiry": "0",
        "default_max_uses": "10",
        "default_valid_hours": "24",
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

# ---------- AUTH DECORATORS ----------
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
                session.clear()
                return redirect(url_for("index", err="invalid"))
            if r["expires_at"] and time.time() > r["expires_at"]:
                session.clear()
                return redirect(url_for("index", err="expired"))
        return f(*a, **k)
    return w

@app.before_request
def gate():
    if request.path.startswith(ADMIN_PATH): return
    if request.path == "/expired": return
    if site_expired(): return redirect("/expired")

# ---------- SHARED UI ----------
BASE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }}</title>
<style>
:root{--bg:#0b1220;--card:#1e293b;--line:#334155;--accent:#38bdf8;--green:#22c55e;--red:#ef4444}
*{box-sizing:border-box;margin:0;padding:0;font-family:system-ui,-apple-system,sans-serif}
body{background:radial-gradient(1200px 600px at 10% -10%,#1e3a5f 0%,var(--bg) 55%);
color:#e2e8f0;min-height:100vh;padding:16px;line-height:1.5}
.wrap{max-width:1080px;margin:0 auto}
.card{background:rgba(30,41,59,.92);border:1px solid var(--line);
border-radius:18px;padding:26px;box-shadow:0 20px 60px rgba(0,0,0,.45)}
h1{color:var(--accent);font-size:22px;margin-bottom:6px;display:flex;gap:8px;align-items:center}
h2{color:var(--accent);font-size:17px;margin:22px 0 10px;display:flex;gap:6px;align-items:center}
.muted{color:#94a3b8;font-size:13px;margin-bottom:10px}
input,select,textarea,button{font-size:15px;padding:11px 13px;border-radius:10px;
border:1px solid #475569;background:#0f172a;color:#e2e8f0;width:100%;margin-bottom:10px;outline:none}
input:focus,select:focus{border-color:var(--accent)}
button{background:var(--accent);color:#0b1220;font-weight:700;border:none;cursor:pointer;transition:.15s}
button:hover{background:#0ea5e9}
.btn-red{background:var(--red);color:#fff}.btn-red:hover{background:#dc2626}
.btn-green{background:var(--green);color:#fff}.btn-green:hover{background:#16a34a}
.btn-ghost{background:transparent;border:1px solid var(--line);color:#cbd5e1;width:auto}
.btn-ghost:hover{background:var(--line)}
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:14px 0}
.stat{background:#0f172a;border:1px solid var(--line);border-radius:12px;padding:14px}
.stat .l{font-size:12px;color:#94a3b8;margin-bottom:4px}
.stat .v{font-size:24px;font-weight:800;color:var(--accent)}
.stat .v.g{color:var(--green)}.stat .v.r{color:var(--red)}.stat .v.o{color:#f59e0b}
table{width:100%;border-collapse:collapse;margin-top:8px;font-size:13.5px}
th,td{padding:9px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}
th{color:#94a3b8;background:#0f172a;font-weight:600}
tr:hover td{background:rgba(56,189,248,.04)}
.badge{display:inline-block;padding:2px 8px;border-radius:6px;background:var(--line);font-size:12px}
.alert{padding:11px;border-radius:10px;margin-bottom:12px;font-size:14px}
.alert.err{background:#7f1d1d;color:#fecaca;border:1px solid #991b1b}
.alert.ok{background:#14532d;color:#bbf7d0;border:1px solid #166534}
nav{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px}
nav a{color:var(--accent);text-decoration:none;padding:7px 12px;border-radius:8px;
border:1px solid var(--line);font-size:13px}
nav a:hover{background:var(--line)}
.code{font-family:ui-monospace,monospace;background:#0f172a;padding:2px 6px;border-radius:5px}
.footer{text-align:center;color:#475569;font-size:12px;margin-top:22px}
a{color:var(--accent)}
</style></head><body><div class="wrap"><div class="card">{{ content|safe }}</div>
<div class="footer">PyHost · Secure File Hosting</div></div></body></html>"""

def page(html, title="PyHost"):
    return render_template_string(BASE, content=html, title=title)

# ---------- USER SIDE ----------
@app.route("/")
def index():
    if session.get("user_ok"):
        return redirect(url_for("dashboard"))
    msg = ""
    if request.args.get("err") == "invalid":
        msg = '<div class="alert err">❌ Galat ya expired access code.</div>'
    elif request.args.get("err") == "expired":
        msg = '<div class="alert err">⏰ Aapka code expire ho gaya hai.</div>'
    html = f"""
    <h1>🔐 PyHost — Guest Access</h1>
    <p class="muted">Apna access code enter karein. Koi signup nahi chahiye.</p>
    {msg}
    <form method="post" action="/access">
      <input name="code" placeholder="Access code" autofocus required>
      <button type="submit">Enter →</button>
    </form>
    <p class="muted" style="margin-top:16px">Yeh code admin ne diya hai. Ek code limited uses aur time ke liye valid hota hai.</p>
    """
    return page(html, "Access")

@app.route("/access", methods=["POST"])
def access():
    code = request.form.get("code", "").strip()
    if not code:
        return redirect(url_for("index", err="invalid"))
    conn = db()
    r = conn.execute("SELECT * FROM access_codes WHERE code=? AND active=1",
                     (code,)).fetchone()
    if not r:
        conn.close()
        return redirect(url_for("index", err="invalid"))
    if r["expires_at"] and time.time() > r["expires_at"]:
        conn.close()
        return redirect(url_for("index", err="expired"))
    if r["max_uses"] > 0 and r["used"] >= r["max_uses"]:
        conn.close()
        return redirect(url_for("index", err="invalid"))
    conn.execute("UPDATE access_codes SET used=used+1 WHERE id=?", (r["id"],))
    conn.execute("""INSERT INTO users (code,ip,first_seen,last_seen)
                    VALUES (?,?,?,?)""",
                 (code, request.remote_addr, time.time(), time.time()))
    conn.commit(); conn.close()

    session["user_ok"] = True
    session["user_code"] = code
    return redirect(url_for("dashboard"))

@app.route("/dashboard")
@user_required
def dashboard():
    ip = request.remote_addr
    code = session.get("user_code", "")
    conn = db()
    conn.execute("UPDATE users SET last_seen=? WHERE code=? AND ip=?",
                 (time.time(), code, ip))
    conn.commit()
    files = conn.execute("""SELECT * FROM files WHERE owner_ip=?
                            ORDER BY id DESC""", (ip,)).fetchall()
    conn.close()
    rows = "".join(
        f"<tr><td>{f['orig']}</td><td>{f['size']//1024} KB</td>"
        f"<td>{f['uploaded_at'][:16]}</td>"
        f"<td><a href='/hosted/{f['filename']}' target='_blank'>Open</a></td></tr>"
        for f in files) or '<tr><td colspan=4 class="muted">Abhi koi file nahi.</td></tr>'
    html = f"""
    <nav>
      <a href="/dashboard">📤 Dashboard</a>
      <a href="/logout">🚪 Logout</a>
    </nav>
    <h1>📤 Aapka Dashboard</h1>
    <p class="muted">Code: <span class="code">{code}</span> · IP: {ip}</p>
    <h2>File Upload Karein</h2>
    <form method="post" action="/upload" enctype="multipart/form-data">
      <input type="file" name="file" required>
      <button class="btn-green" type="submit">⬆️ Upload & Host</button>
    </form>
    <p class="muted">Max {MAX_UPLOAD_MB} MB per file.</p>
    <h2>Aapki Files</h2>
    <table>
      <tr><th>Name</th><th>Size</th><th>Time</th><th>Link</th></tr>
      {rows}
    </table>
    """
    return page(html, "Dashboard")

@app.route("/upload", methods=["POST"])
@user_required
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return redirect(url_for("dashboard"))
    safe = secure_filename(f.filename)
    stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    fname = f"{stamp}_{safe}"
    path = os.path.join(UPLOAD_DIR, fname)
    f.save(path)
    size = os.path.getsize(path)
    conn = db()
    conn.execute("""INSERT INTO files
        (filename,orig,owner_code,owner_ip,size,uploaded_at)
        VALUES (?,?,?,?,?,?)""",
        (fname, f.filename, session.get("user_code",""), request.remote_addr,
         size, datetime.datetime.now().isoformat()))
    conn.commit(); conn.close()
    return redirect(url_for("dashboard"))

@app.route("/hosted/<path:name>")
def hosted(name):
    if site_expired():
        abort(410)
    return send_from_directory(UPLOAD_DIR, name)

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/expired")
def expired_page():
    return page("""<h1>⏰ Site Expired</h1>
    <p class="muted">Yeh site admin-set time limit ke baad band ho gayi hai.</p>""",
    "Expired")

# ---------- ADMIN SIDE ----------
@app.route(ADMIN_PATH + "/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("code") == cfg("admin_code"):
            session["admin"] = True
            return redirect(ADMIN_PATH)
        return page("""<h1>🔒 Admin</h1>
            <div class="alert err">Galat admin code.</div>
            <form method="post"><input name="code" type="password" autofocus>
            <button>Login</button></form>""", "Admin")
    return page("""<h1>🔒 Admin Login</h1>
        <form method="post">
        <input name="code" type="password" placeholder="Admin code" autofocus required>
        <button>Login</button></form>""", "Admin")

@app.route(ADMIN_PATH + "/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(ADMIN_PATH + "/login")

@app.route(ADMIN_PATH)
@admin_required
def admin_panel():
    now = time.time()
    conn = db()
    active = conn.execute("SELECT COUNT(*) c FROM users WHERE last_seen>?",
                          (now-300,)).fetchone()["c"]
    total  = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    files  = conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"]
    codes  = conn.execute("SELECT COUNT(*) c FROM access_codes WHERE active=1").fetchone()["c"]

    file_rows = "".join(f"""<tr>
        <td>{f['id']}</td><td>{f['orig']}</td><td>{f['owner_ip']}</td>
        <td>{f['size']//1024} KB</td><td>{f['uploaded_at'][:16]}</td>
        <td><a href="{ADMIN_PATH}/delete/{f['id']}"
          onclick="return confirm('Delete?')" style="color:var(--red)">🗑</a></td>
        </tr>""" for f in conn.execute(
            "SELECT * FROM files ORDER BY id DESC LIMIT 100").fetchall()) or \
        '<tr><td colspan=6 class="muted">No files</td></tr>'

    code_rows = "".join(f"""<tr>
        <td><span class="code">{c['code']}</span></td>
        <td>{c['used']}/{c['max_uses'] if c['max_uses'] else '∞'}</td>
        <td>{(datetime.datetime.fromtimestamp(c['expires_at']).strftime('%d-%m %H:%M')
              if c['expires_at'] else 'Never')}</td>
        <td>{'✅ Active' if c['active'] else '⛔ Off'}</td>
        <td><a href="{ADMIN_PATH}/revoke/{c['id']}" style="color:var(--red)">Revoke</a></td>
        </tr>""" for c in conn.execute(
            "SELECT * FROM access_codes ORDER BY id DESC LIMIT 50").fetchall()) or \
        '<tr><td colspan=5 class="muted">No access codes</td></tr>'

    user_rows = "".join(f"""<tr>
        <td><span class="code">{u['code']}</span></td><td>{u['ip']}</td>
        <td>{'🟢 Online' if now-u['last_seen']<300 else '⚪ Offline'}</td>
        <td>{datetime.datetime.fromtimestamp(u['last_seen']).strftime('%d-%m %H:%M')}</td>
        </tr>""" for u in conn.execute(
            "SELECT * FROM users ORDER BY last_seen DESC LIMIT 50").fetchall()) or \
        '<tr><td colspan=4 class="muted">No users</td></tr>'

    detail_rows = "".join(f"""<tr><td>{d['name']}</td><td>{d['email']}</td>
        <td>{d['note']}</td><td>{d['created_at'][:16]}</td></tr>"""
        for d in conn.execute(
            "SELECT * FROM user_details ORDER BY id DESC LIMIT 30").fetchall()) or \
        '<tr><td colspan=4 class="muted">No records</td></tr>'
    conn.close()

    exp = int(cfg("site_expiry", "0"))
    exp_str = datetime.datetime.fromtimestamp(exp).strftime("%d-%m-%Y %H:%M") if exp else "♾️ Never"

    html = f"""
    <nav>
      <a href="{ADMIN_PATH}">🔄 Refresh</a>
      <a href="{ADMIN_PATH}/generate">🔑 New Code</a>
      <a href="{ADMIN_PATH}/settings">⚙️ Settings</a>
      <a href="{ADMIN_PATH}/logout">🚪 Logout</a>
    </nav>
    <h1>🛡️ Admin Control Panel</h1>
    <div class="stats">
      <div class="stat"><div class="l">Active (5m)</div><div class="v g">{active}</div></div>
      <div class="stat"><div class="l">Total Users</div><div class="v">{total}</div></div>
      <div class="stat"><div class="l">Files</div><div class="v">{files}</div></div>
      <div class="stat"><div class="l">Active Codes</div><div class="v o">{codes}</div></div>
      <div class="stat"><div class="l">Site Expiry</div>
        <div class="v r" style="font-size:13px">{exp_str}</div></div>
    </div>

    <h2>🔑 Access Codes</h2>
    <form method="post" action="{ADMIN_PATH}/generate" class="row">
      <input name="max_uses" type="number" placeholder="Max uses (0=∞)" value="{cfg('default_max_uses')}">
      <input name="hours" type="number" placeholder="Valid hours (0=∞)" value="{cfg('default_valid_hours')}">
      <button type="submit">Generate Code</button>
    </form>
    <table>
      <tr><th>Code</th><th>Uses</th><th>Expiry</th><th>Status</th><th>Action</th></tr>
      {code_rows}
    </table>

    <h2>👥 Users</h2>
    <table>
      <tr><th>Code</th><th>IP</th><th>Status</th><th>Last Seen</th></tr>
      {user_rows}
    </table>

    <h2>📁 All Uploaded Files</h2>
    <table>
      <tr><th>ID</th><th>Name</th><th>Owner IP</th><th>Size</th><th>Time</th><th>Del</th></tr>
      {file_rows}
    </table>

    <h2>📝 User Details (Manual Records)</h2>
    <form method="post" action="{ADMIN_PATH}/add_detail"
          style="display:grid;grid-template-columns:1fr 1fr 2fr auto;gap:8px;align-items:end">
      <input name="name" placeholder="Name">
      <input name="email" placeholder="Email">
      <input name="note" placeholder="Note">
      <button type="submit">➕ Add</button>
    </form>
    <table>
      <tr><th>Name</th><th>Email</th><th>Note</th><th>Time</th></tr>
      {detail_rows}
    </table>
    """
    return page(html, "Admin Panel")

@app.route(ADMIN_PATH + "/generate", methods=["POST"])
@admin_required
def generate_code():
    mu = int(request.form.get("max_uses", 10) or 10)
    hrs = float(request.form.get("hours", 24) or 24)
    code = str(secrets.randbelow(9000) + 1000)   # 4-digit
    expires = (time.time() + hrs*3600) if hrs > 0 else None
    conn = db()
    conn.execute("""INSERT INTO access_codes
        (code,max_uses,used,expires_at,active,created_at)
        VALUES (?,?,0,?,1,?)""",
        (code, mu, expires, datetime.datetime.now().isoformat()))
    conn.commit(); conn.close()
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
            val = request.form.get("expiry","")
            try:
                dt = datetime.datetime.strptime(val, "%Y-%m-%dT%H:%M")
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
        return redirect(ADMIN_PATH + "/settings")

    exp = int(cfg("site_expiry", "0"))
    exp_val = datetime.datetime.fromtimestamp(exp).strftime("%Y-%m-%dT%H:%M") if exp else ""

    html = f"""
    <nav><a href="{ADMIN_PATH}">← Back</a></nav>
    <h1>⚙️ Settings</h1>
    <h2>⏰ Site Auto-Expiry</h2>
    <p class="muted">Is time ke baad poori site band ho jaegi.</p>
    <form method="post">
      <input type="hidden" name="action" value="expiry">
      <input type="datetime-local" name="expiry" value="{exp_val}">
      <button class="btn-red">Set Expiry</button>
    </form>
    <form method="post" style="margin-top:6px">
      <input type="hidden" name="action" value="clear_expiry">
      <button class="btn-ghost">♾️ Never Expire</button>
    </form>

    <h2>🔑 Default Code Settings</h2>
    <form method="post">
      <input type="hidden" name="action" value="defaults">
      <input name="mu" type="number" placeholder="Default max uses" value="{cfg('default_max_uses')}">
      <input name="hrs" type="number" placeholder="Default valid hours" value="{cfg('default_valid_hours')}">
      <button>Save Defaults</button>
    </form>

    <h2>🛡️ Change Admin Code</h2>
    <p class="muted">Current admin code hidden hai.</p>
    <form method="post">
      <input type="hidden" name="action" value="admin_code">
      <input name="code" placeholder="New admin code">
      <button>Update Admin Code</button>
    </form>
    """
    return page(html, "Settings")

# ---------- RUN ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
