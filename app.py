import os, re, sqlite3, uuid, subprocess, json, time
from pathlib import Path
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, session, abort, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("DATA_DIR", ROOT / "data"))
UPLOAD_ROOT = Path(os.environ.get("UPLOAD_DIR", ROOT / "uploads"))
DB_PATH = Path(os.environ.get("DATABASE_PATH", DATA / "rocks.db"))
DATA.mkdir(parents=True, exist_ok=True)
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-this-secret")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("COOKIE_SECURE", "0") == "1"
CHANNEL_URL = "https://t.me/rocks_update"
RUNNER_MODE = os.environ.get("RUNNER_MODE", "docker").lower()
DOCKER_BIN = os.environ.get("DOCKER_BIN", "docker")
MAX_BOTS_PER_USER = int(os.environ.get("MAX_BOTS_PER_USER", "5"))

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS bots (
            id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL,
            folder TEXT NOT NULL, main_file TEXT NOT NULL DEFAULT 'bot.py',
            status TEXT NOT NULL DEFAULT 'Stopped', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('maintenance','0')")

def docker_available():
    try:
        p = subprocess.run([DOCKER_BIN, "info"], capture_output=True, text=True, timeout=5)
        return p.returncode == 0
    except Exception:
        return False

def current_user():
    if not session.get("user_id"): return None
    with db() as c:
        return c.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()

def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not current_user():
            flash("Please log in to continue.", "error")
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapped

def owner_bot(bot_id):
    user = current_user()
    with db() as c:
        bot = c.execute("SELECT * FROM bots WHERE id=? AND user_id=?", (bot_id, user["id"])).fetchone()
    if not bot: abort(404)
    return bot

def run_docker(args, timeout=60):
    return subprocess.run([DOCKER_BIN] + args, capture_output=True, text=True, timeout=timeout)

def container_name(bot_id):
    return "rocksbot-" + re.sub(r"[^a-zA-Z0-9_-]", "", bot_id)[:24]

def runner_status(bot):
    if RUNNER_MODE == "demo": return bot["status"]
    if not docker_available(): return "Runner offline"
    try:
        p = run_docker(["inspect", "-f", "{{.State.Running}}", container_name(bot["id"])], timeout=8)
        if p.returncode == 0 and p.stdout.strip() == "true": return "Running"
        if p.returncode == 0: return "Stopped"
    except Exception: pass
    return "Stopped"

def build_image(bot):
    folder = Path(bot["folder"])
    dockerfile = folder / "Dockerfile.rocks"
    dockerfile.write_text(
        "FROM python:3.12-slim\n"
        "ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1\n"
        "WORKDIR /app\n"
        "COPY requirements.txt /app/requirements.txt\n"
        "RUN python -m pip install --no-cache-dir -r /app/requirements.txt\n"
        "COPY . /app\n"
        "RUN useradd --create-home --uid 10001 botuser && chown -R botuser:botuser /app\n"
        "USER 10001:10001\n"
        "CMD [\"python\", \"/app/" + bot["main_file"].replace('"','') + "\"]\n",
        encoding="utf-8")
    tag = "rocksbot-image-" + bot["id"][:24].lower()
    p = run_docker(["build", "--pull", "-t", tag, "-f", str(dockerfile), str(folder)], timeout=900)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "Docker image build failed")[-3500:])
    return tag

def start_real_bot(bot):
    if not docker_available():
        raise RuntimeError("Docker runner is unavailable on this host. Connect a Docker-capable runner first.")
    name = container_name(bot["id"])
    run_docker(["rm", "-f", name], timeout=15)
    image = build_image(bot)
    args = ["run", "-d", "--name", name,
            "--restart", "unless-stopped",
            "--memory", "256m", "--memory-swap", "256m",
            "--cpus", "0.50", "--pids-limit", "64",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m",
            "--user", "10001:10001", "--init",
            # Network is enabled for Telegram API access. This is not a complete security boundary.
            image]
    p = run_docker(args, timeout=30)
    if p.returncode != 0: raise RuntimeError((p.stderr or p.stdout or "Container start failed")[-2500:])

def stop_real_bot(bot):
    if docker_available():
        run_docker(["stop", "-t", "5", container_name(bot["id"])], timeout=15)
        run_docker(["rm", "-f", container_name(bot["id"])], timeout=15)

def logs_real_bot(bot):
    if not docker_available(): return "Runner offline. Logs are unavailable until a Docker runner is connected."
    p = run_docker(["logs", "--tail", "150", container_name(bot["id"])], timeout=10)
    return (p.stdout + "\n" + p.stderr)[-12000:] or "No logs yet."

def sync_status(bot_id):
    with db() as c:
        bot = c.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
        if not bot: return
        status = runner_status(bot)
        c.execute("UPDATE bots SET status=? WHERE id=?", (status, bot_id))

@app.context_processor
def inject_globals():
    return {"channel_url": CHANNEL_URL, "site_name": "ROCKS HOSTING BOT",
            "logged_user": current_user(), "runner_mode": RUNNER_MODE,
            "runner_online": RUNNER_MODE == "demo" or docker_available()}

@app.get("/")
def home():
    if current_user(): return redirect(url_for("dashboard"))
    return render_template("index.html")

@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        if not re.fullmatch(r"[a-z0-9_]{3,24}", username):
            flash("Username must be 3–24 characters: letters, numbers or underscore.", "error")
        elif len(password) < 10:
            flash("Use a password with at least 10 characters.", "error")
        else:
            try:
                with db() as c:
                    c.execute("INSERT INTO users(username,password_hash) VALUES(?,?)",
                              (username, generate_password_hash(password)))
                flash("Account created. Please log in.", "success")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                flash("That username is already taken.", "error")
    return render_template("auth.html", mode="register")

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        with db() as c:
            user = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            return redirect(url_for("dashboard"))
        flash("Incorrect username or password.", "error")
    return render_template("auth.html", mode="login")

@app.post("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("home"))

@app.get("/dashboard")
@login_required
def dashboard():
    user = current_user()
    with db() as c:
        rows = c.execute("SELECT * FROM bots WHERE user_id=? ORDER BY created_at DESC", (user["id"],)).fetchall()
    bot_list = []
    for row in rows:
        b = dict(row)
        b["status"] = runner_status(b)
        bot_list.append(b)
    return render_template("dashboard.html", bots=bot_list, max_bots=MAX_BOTS_PER_USER)

@app.post("/bots")
@login_required
def create_bot():
    user = current_user()
    name = (request.form.get("bot_name") or "").strip()[:60]
    py = request.files.get("python_file")
    req = request.files.get("requirements_file")
    if not name or not py or not py.filename or not req or not req.filename:
        flash("Bot name, Python file and requirements.txt are required.", "error")
        return redirect(url_for("dashboard") + "#upload")
    if Path(py.filename).suffix.lower() != ".py":
        flash("Main bot file must be a .py file.", "error"); return redirect(url_for("dashboard") + "#upload")
    if Path(req.filename).name.lower() != "requirements.txt":
        flash("Dependency file must be named requirements.txt.", "error"); return redirect(url_for("dashboard") + "#upload")
    with db() as c:
        count = c.execute("SELECT COUNT(*) FROM bots WHERE user_id=?", (user["id"],)).fetchone()[0]
    if count >= MAX_BOTS_PER_USER:
        flash(f"Demo limit: {MAX_BOTS_PER_USER} bots per account.", "error")
        return redirect(url_for("dashboard") + "#upload")
    bot_id = uuid.uuid4().hex[:12]
    folder = UPLOAD_ROOT / str(user["id"]) / bot_id
    folder.mkdir(parents=True, exist_ok=False)
    py_name = secure_filename(py.filename) or "bot.py"
    if not py_name.endswith(".py"): py_name = "bot.py"
    py.save(folder / "bot.py")
    req.save(folder / "requirements.txt")
    with db() as c:
        c.execute("INSERT INTO bots(id,user_id,name,folder,main_file,status) VALUES(?,?,?,?,?,?)",
                  (bot_id,user["id"],name,str(folder),"bot.py","Stopped"))
    flash("Bot project uploaded.", "success")
    return redirect(url_for("dashboard"))

@app.post("/bots/<bot_id>/<action>")
@login_required
def bot_action(bot_id, action):
    bot = owner_bot(bot_id)
    if action not in {"start","stop","restart"}: abort(404)
    if RUNNER_MODE == "demo":
        status = {"start":"Running (demo)","stop":"Stopped","restart":"Running (demo)"}[action]
        with db() as c: c.execute("UPDATE bots SET status=? WHERE id=?", (status,bot_id))
        flash(f"{action.title()} preview completed. Demo mode does not execute uploaded code.", "success")
    else:
        try:
            if action == "stop": stop_real_bot(bot)
            else:
                if action == "restart": stop_real_bot(bot)
                start_real_bot(bot)
            sync_status(bot_id)
            flash(f"Bot {action} command completed.", "success")
        except Exception as e:
            flash(f"Runner error: {str(e)[:450]}", "error")
    return redirect(url_for("dashboard") + "#my-bots")

@app.post("/bots/<bot_id>/update")
@login_required
def update_bot(bot_id):
    bot = owner_bot(bot_id)
    py = request.files.get("python_file")
    req = request.files.get("requirements_file")
    if (not py or not py.filename) and (not req or not req.filename):
        flash("Choose a new Python file and/or requirements.txt.", "error")
        return redirect(url_for("dashboard") + "#my-bots")
    folder = Path(bot["folder"])
    if py and py.filename:
        if Path(py.filename).suffix.lower() != ".py":
            flash("Updated main file must be .py.", "error"); return redirect(url_for("dashboard") + "#my-bots")
        # Keep a fixed safe entry filename to avoid path traversal.
        py.save(folder / "bot.py")
    if req and req.filename:
        if Path(req.filename).name.lower() != "requirements.txt":
            flash("Dependency file must be named requirements.txt.", "error"); return redirect(url_for("dashboard") + "#my-bots")
        req.save(folder / "requirements.txt")
    with db() as c:
        c.execute("UPDATE bots SET main_file='bot.py', status='Updated — stopped', updated_at=CURRENT_TIMESTAMP WHERE id=?", (bot_id,))
    flash("Files updated. Restart the bot to rebuild dependencies and apply changes.", "success")
    return redirect(url_for("dashboard") + "#my-bots")

@app.get("/bots/<bot_id>/logs")
@login_required
def bot_logs(bot_id):
    bot = owner_bot(bot_id)
    logs = logs_real_bot(bot) if RUNNER_MODE != "demo" else "Demo mode: no process has been started, so there are no real logs."
    return render_template("logs.html", bot=bot, logs=logs)

@app.post("/bots/<bot_id>/delete")
@login_required
def delete_bot(bot_id):
    bot = owner_bot(bot_id)
    try: stop_real_bot(bot)
    except Exception: pass
    import shutil
    shutil.rmtree(bot["folder"], ignore_errors=True)
    with db() as c: c.execute("DELETE FROM bots WHERE id=?", (bot_id,))
    flash("Bot project deleted.", "success")
    return redirect(url_for("dashboard"))

@app.get("/health")
def health():
    return jsonify(status="ok", service="ROCKS HOSTING BOT", runner_mode=RUNNER_MODE,
                   docker_available=docker_available() if RUNNER_MODE != "demo" else False)

@app.errorhandler(413)
def too_large(_):
    flash("Upload is too large. Maximum request size is 20 MB.", "error")
    return redirect(url_for("dashboard"))

init_db()
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT","10000")), debug=False)
