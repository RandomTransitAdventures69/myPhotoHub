import json
import hmac
import os
import secrets
import shutil
import sqlite3
import tempfile
import time
import zipfile
import uuid
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_from_directory, send_file, session, url_for
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

ROOT = Path(__file__).resolve().parent
# All Stillroom releases use one stable data directory, independent of where the
# application ZIP is extracted. Override with STILLROOM_DATA_DIR if desired.
_default_data = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Stillroom"
DATA_ROOT = Path(r"Z:\StillroomData")
# One-time migration for an older copy whose data lived beside app.py.
if not DATA_ROOT.exists():
    legacy_dirs = {"instance": ROOT / "instance", "photos": ROOT / "photos", "thumbnails": ROOT / "thumbnails"}
    if any(path.exists() for path in legacy_dirs.values()):
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        for name, old_path in legacy_dirs.items():
            if old_path.exists() and not (DATA_ROOT / name).exists():
                shutil.copytree(old_path, DATA_ROOT / name) if old_path.is_dir() else shutil.copy2(old_path, DATA_ROOT / name)
INSTANCE = DATA_ROOT / "instance"
UPLOAD_DIR = DATA_ROOT / "photos"
THUMB_DIR = DATA_ROOT / "thumbnails"
AVATAR_DIR = DATA_ROOT / "avatars"
BACKUP_DIR = DATA_ROOT / "backups"
DB_PATH = INSTANCE / "stillroom.db"
CONFIG_PATH = INSTANCE / "config.json"
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
MAX_UPLOAD_MB = int(os.environ.get("STILLROOM_MAX_UPLOAD_MB", "512"))
STARTED_AT = time.time()

for folder in (DATA_ROOT, INSTANCE, UPLOAD_DIR, THUMB_DIR, AVATAR_DIR, BACKUP_DIR):
    folder.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


def read_config():
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    config = {"secret_key": secrets.token_hex(32), "password_hash": None}
    write_config(config)
    return config


def write_config(config):
    temp = CONFIG_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(config, indent=2), encoding="utf-8")
    temp.replace(CONFIG_PATH)


CONFIG = read_config()
MAX_UPLOAD_MB = int(CONFIG.get("max_upload_mb", MAX_UPLOAD_MB))
app.secret_key = CONFIG["secret_key"]
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
app.jinja_env.filters["datetimeformat"] = lambda value: datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")


@app.context_processor
def inject_template_helpers():
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_urlsafe(32)
    return {"csrf_token": session["_csrf_token"]}


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db():
    """Create tables and migrate the original single-password database."""
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id INTEGER,
            actor_name TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS photos (
            id TEXT PRIMARY KEY,
            owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            original_name TEXT NOT NULL,
            title TEXT NOT NULL,
            uploaded_at TEXT NOT NULL,
            taken_at TEXT,
            width INTEGER,
            height INTEGER,
            favorite INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS albums (
            id TEXT PRIMARY KEY,
            owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS album_photos (
            album_id TEXT NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
            photo_id TEXT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
            PRIMARY KEY (album_id, photo_id)
        );
        """)
        # Migrate account profiles from earlier releases.
        user_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if "avatar_filename" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN avatar_filename TEXT")
        # Migrate databases made by the first Stillroom version.
        photo_cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)")}
        if "owner_id" not in photo_cols:
            conn.execute("ALTER TABLE photos ADD COLUMN owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
        album_cols = {r[1] for r in conn.execute("PRAGMA table_info(albums)")}
        if "owner_id" not in album_cols:
            conn.execute("ALTER TABLE albums ADD COLUMN owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
        # If an old single-password setup exists, preserve it as the admin account.
        if CONFIG.get("password_hash") and not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, is_admin, active, created_at) VALUES (?, ?, 1, 1, ?)",
                ("admin", CONFIG["password_hash"], datetime.now().isoformat(timespec="seconds"))
            )
            admin_id = cur.lastrowid
            conn.execute("UPDATE photos SET owner_id=? WHERE owner_id IS NULL", (admin_id,))
            conn.execute("UPDATE albums SET owner_id=? WHERE owner_id IS NULL", (admin_id,))
            CONFIG["password_hash"] = None
            write_config(CONFIG)
        # If tables were already present but have no accounts, leave first-run setup enabled.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_owner ON photos(owner_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_albums_owner ON albums(owner_id)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_albums_owner_name ON albums(owner_id, name)")


init_db()


def log_activity(action, detail="", actor=None):
    actor = actor or current_user()
    actor_id = actor["id"] if actor else None
    actor_name = actor["username"] if actor else "system"
    with db() as conn:
        conn.execute("INSERT INTO activity_log (actor_id, actor_name, action, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                     (actor_id, actor_name, action[:80], detail[:300], datetime.now().isoformat(timespec="seconds")))


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    with db() as conn:
        return conn.execute("SELECT id, username, is_admin, active FROM users WHERE id=? AND active=1", (user_id,)).fetchone()


def logged_in(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        with db() as conn:
            has_users = conn.execute("SELECT 1 FROM users WHERE active=1 LIMIT 1").fetchone()
        if not has_users:
            return redirect(url_for("setup"))
        user = current_user()
        if not user:
            session.clear()
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    @logged_in
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user or not user["is_admin"]:
            abort(403)
        if request.method == "POST" and not hmac.compare_digest(request.form.get("csrf_token", ""), session.get("_csrf_token", "")):
            abort(400)
        return view(*args, **kwargs)
    return wrapped


def owns_photo(conn, photo_id, user_id):
    row = conn.execute("SELECT * FROM photos WHERE id=? AND owner_id=?", (photo_id, user_id)).fetchone()
    if not row:
        abort(404)
    return row


def owns_album(conn, album_id, user_id):
    row = conn.execute("SELECT * FROM albums WHERE id=? AND owner_id=?", (album_id, user_id)).fetchone()
    if not row:
        abort(404)
    return row

def photo_dict(row):
    item = dict(row)
    item["date_label"] = (item.get("taken_at") or item.get("uploaded_at") or "")[:10]
    return item


@app.route("/setup", methods=["GET", "POST"])
def setup():
    with db() as conn:
        has_users = conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
    if has_users:
        return redirect(url_for("login"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if len(username) < 3 or len(username) > 32 or not username.replace("_", "").replace("-", "").isalnum():
            flash("Username must be 3–32 characters using letters, numbers, _ or -.", "error")
        elif len(password) < 12:
            flash("Use at least 12 characters for your password.", "error")
        elif password != confirm:
            flash("Those passwords don't match.", "error")
        else:
            with db() as conn:
                cur = conn.execute("INSERT INTO users (username, password_hash, is_admin, active, created_at) VALUES (?, ?, 1, 1, ?)",
                                   (username, generate_password_hash(password), datetime.now().isoformat(timespec="seconds")))
                admin_id = cur.lastrowid
                conn.execute("UPDATE photos SET owner_id=? WHERE owner_id IS NULL", (admin_id,))
                conn.execute("UPDATE albums SET owner_id=? WHERE owner_id IS NULL", (admin_id,))
            CONFIG["password_hash"] = None
            write_config(CONFIG)
            flash("Admin account created. Sign in to continue.", "success")
            return redirect(url_for("login"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    with db() as conn:
        has_users = conn.execute("SELECT 1 FROM users WHERE active=1 LIMIT 1").fetchone()
    if not has_users:
        return redirect(url_for("setup"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        with db() as conn:
            user = conn.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE AND active=1", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["is_admin"] = bool(user["is_admin"])
            session["avatar_filename"] = user["avatar_filename"]
            target = request.args.get("next", "")
            return redirect(target if target.startswith("/") and not target.startswith("//") else url_for("index"))
        flash("Incorrect username or password, or this account is disabled.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/users", methods=["GET", "POST"])
@admin_required
def users():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if len(username) < 3 or len(username) > 32 or not username.replace("_", "").replace("-", "").isalnum():
            flash("Username must be 3–32 characters using letters, numbers, _ or -.", "error")
        elif len(password) < 12:
            flash("Use a password of at least 12 characters.", "error")
        else:
            try:
                with db() as conn:
                    conn.execute("INSERT INTO users (username, password_hash, is_admin, active, created_at) VALUES (?, ?, 0, 1, ?)",
                                 (username, generate_password_hash(password), datetime.now().isoformat(timespec="seconds")))
                flash(f"Account “{username}” created.", "success")
                log_activity("user_created", f"Created account {username}")
            except sqlite3.IntegrityError:
                flash("That username is already taken.", "error")
        return redirect(url_for("admin_dashboard"))
    with db() as conn:
        people = conn.execute("SELECT u.id, u.username, u.is_admin, u.active, u.created_at, COUNT(p.id) AS photo_count FROM users u LEFT JOIN photos p ON p.owner_id=u.id GROUP BY u.id ORDER BY u.is_admin DESC, u.username COLLATE NOCASE").fetchall()
    return render_template("users.html", people=people)


@app.route("/users/<int:user_id>/toggle", methods=["POST"])
@admin_required
def user_toggle(user_id):
    actor = current_user()
    if user_id == actor["id"]:
        flash("You can't disable the account you're currently using.", "error")
        return redirect(url_for("users"))
    with db() as conn:
        target = conn.execute("SELECT id, username, is_admin, active FROM users WHERE id=?", (user_id,)).fetchone()
        if not target:
            abort(404)
        if target["is_admin"] and target["active"]:
            active_admins = conn.execute("SELECT COUNT(*) FROM users WHERE is_admin=1 AND active=1").fetchone()[0]
            if active_admins <= 1:
                flash("You can't disable the last active admin.", "error")
                return redirect(url_for("users"))
        new_active = 0 if target["active"] else 1
        conn.execute("UPDATE users SET active=? WHERE id=?", (new_active, user_id))
    log_activity("user_enabled" if new_active else "user_disabled", f"{'Enabled' if new_active else 'Disabled'} account {target['username']}")
    flash(f"Account “{target['username']}” {'enabled' if new_active else 'disabled'}.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/profile", methods=["GET", "POST"])
@logged_in
def profile():
    user = current_user()
    if request.method == "POST":
        if not hmac.compare_digest(request.form.get("csrf_token", ""), session.get("_csrf_token", "")):
            abort(400)
        action = request.form.get("action", "avatar")
        if action == "password":
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            confirm = request.form.get("confirm_password", "")
            password_changed = False
            with db() as conn:
                row = conn.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
                if not row or not check_password_hash(row["password_hash"], current_password):
                    flash("Your current password wasn't correct.", "error")
                elif len(new_password) < 12:
                    flash("Use a password of at least 12 characters.", "error")
                elif new_password != confirm:
                    flash("The new passwords don't match.", "error")
                else:
                    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new_password), user["id"]))
                    password_changed = True
                    flash("Password updated.", "success")
            if password_changed:
                log_activity("password_changed", "Changed own password", user)
            return redirect(url_for("profile"))
        avatar = request.files.get("avatar")
        if not avatar or not avatar.filename:
            flash("Choose an image for your profile picture.", "error")
            return redirect(url_for("profile"))
        ext = Path(avatar.filename).suffix.lower()
        if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
            flash("Profile pictures must be JPG, PNG, or WebP.", "error")
            return redirect(url_for("profile"))
        avatar_name = f"user-{user['id']}-{uuid.uuid4().hex}.jpg"
        target = AVATAR_DIR / avatar_name
        try:
            with Image.open(avatar.stream) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
                image.thumbnail((512, 512))
                image.save(target, "JPEG", quality=88)
        except (UnidentifiedImageError, OSError, ValueError):
            target.unlink(missing_ok=True)
            flash("That file isn't a supported image.", "error")
            return redirect(url_for("profile"))
        with db() as conn:
            old = conn.execute("SELECT avatar_filename FROM users WHERE id=?", (user["id"],)).fetchone()
            conn.execute("UPDATE users SET avatar_filename=? WHERE id=?", (avatar_name, user["id"]))
        if old and old["avatar_filename"]:
            (AVATAR_DIR / old["avatar_filename"]).unlink(missing_ok=True)
        session["avatar_filename"] = avatar_name
        log_activity("profile_picture_updated", "Updated profile picture", user)
        flash("Profile picture updated.", "success")
        return redirect(url_for("profile"))
    with db() as conn:
        person = conn.execute("SELECT id, username, is_admin, active, created_at, avatar_filename FROM users WHERE id=?", (user["id"],)).fetchone()
    return render_template("profile.html", person=person)


@app.route("/avatar/<int:user_id>")
@logged_in
def avatar(user_id):
    viewer = current_user()
    if viewer["id"] != user_id and not viewer["is_admin"]:
        abort(404)
    with db() as conn:
        person = conn.execute("SELECT avatar_filename FROM users WHERE id=?", (user_id,)).fetchone()
    if not person or not person["avatar_filename"]:
        abort(404)
    return send_from_directory(AVATAR_DIR, person["avatar_filename"], conditional=True)


@app.route("/admin")
@admin_required
def admin_dashboard():
    with db() as conn:
        people = conn.execute("""SELECT u.id, u.username, u.is_admin, u.active, u.created_at, u.avatar_filename,
            COUNT(DISTINCT p.id) AS photo_count, COALESCE(SUM(LENGTH(p.filename)),0) AS unused
            FROM users u LEFT JOIN photos p ON p.owner_id=u.id GROUP BY u.id
            ORDER BY u.is_admin DESC, u.username COLLATE NOCASE""").fetchall()
        photo_count = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
        album_count = conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0]
        activity = conn.execute("SELECT * FROM activity_log ORDER BY id DESC LIMIT 30").fetchall()
    disk = shutil.disk_usage(DATA_ROOT)
    photo_bytes = sum(f.stat().st_size for f in UPLOAD_DIR.iterdir() if f.is_file()) if UPLOAD_DIR.exists() else 0
    thumb_bytes = sum(f.stat().st_size for f in THUMB_DIR.iterdir() if f.is_file()) if THUMB_DIR.exists() else 0
    avatar_bytes = sum(f.stat().st_size for f in AVATAR_DIR.iterdir() if f.is_file()) if AVATAR_DIR.exists() else 0
    backups = sorted((f for f in BACKUP_DIR.glob("stillroom-backup-*.zip") if f.is_file()), key=lambda f: f.stat().st_mtime, reverse=True)
    return render_template("admin.html", people=people, photo_count=photo_count, album_count=album_count,
        disk_total=disk.total, disk_used=disk.total-disk.free, disk_free=disk.free,
        photo_bytes=photo_bytes, thumb_bytes=thumb_bytes, avatar_bytes=avatar_bytes,
        uptime=int(time.time()-STARTED_AT), max_upload_mb=MAX_UPLOAD_MB, activity=activity, backups=backups, data_root_label=str(DATA_ROOT))


@app.route("/admin/users/<int:user_id>/password", methods=["POST"])
@admin_required
def admin_reset_password(user_id):
    password = request.form.get("password", "")
    if len(password) < 12:
        flash("Temporary passwords must be at least 12 characters.", "error")
        return redirect(url_for("admin_dashboard"))
    with db() as conn:
        target = conn.execute("SELECT id, username FROM users WHERE id=?", (user_id,)).fetchone()
        if not target:
            abort(404)
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(password), user_id))
    log_activity("password_reset", f"Reset password for {target['username']}")
    flash(f"Password reset for {target['username']}.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users/<int:user_id>/role", methods=["POST"])
@admin_required
def admin_toggle_role(user_id):
    actor = current_user()
    with db() as conn:
        target = conn.execute("SELECT id, username, is_admin FROM users WHERE id=?", (user_id,)).fetchone()
        if not target:
            abort(404)
        if target["id"] == actor["id"] and target["is_admin"]:
            flash("You can't remove your own admin role while signed in. Promote another admin first, then use that account.", "error")
            return redirect(url_for("admin_dashboard"))
        new_role = 0 if target["is_admin"] else 1
        if target["is_admin"] and new_role == 0:
            active_admins = conn.execute("SELECT COUNT(*) FROM users WHERE is_admin=1 AND active=1").fetchone()[0]
            if active_admins <= 1 and target["id"] == actor["id"]:
                flash("You can't remove your own admin role while you're the only active admin.", "error")
                return redirect(url_for("admin_dashboard"))
        conn.execute("UPDATE users SET is_admin=? WHERE id=?", (new_role, user_id))
    log_activity("role_changed", f"{'Promoted' if new_role else 'Demoted'} {target['username']} {'to admin' if new_role else 'to user'}")
    flash(f"Updated role for {target['username']}.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    actor = current_user()
    if actor["id"] == user_id:
        flash("You can't delete the account you're currently using.", "error")
        return redirect(url_for("admin_dashboard"))
    with db() as conn:
        target = conn.execute("SELECT id, username, is_admin, avatar_filename FROM users WHERE id=?", (user_id,)).fetchone()
        if not target:
            abort(404)
        if target["is_admin"]:
            active_admins = conn.execute("SELECT COUNT(*) FROM users WHERE is_admin=1 AND active=1").fetchone()[0]
            if active_admins <= 1:
                flash("You can't delete the last active admin.", "error")
                return redirect(url_for("admin_dashboard"))
        photo_rows = conn.execute("SELECT id, filename FROM photos WHERE owner_id=?", (user_id,)).fetchall()
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    for row in photo_rows:
        (UPLOAD_DIR / row["filename"]).unlink(missing_ok=True)
        (THUMB_DIR / f"{row['id']}.jpg").unlink(missing_ok=True)
    if target["avatar_filename"]:
        (AVATAR_DIR / target["avatar_filename"]).unlink(missing_ok=True)
    log_activity("user_deleted", f"Deleted account {target['username']} and {len(photo_rows)} photos", actor)
    flash(f"Deleted {target['username']} and their {len(photo_rows)} photos.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users/<int:user_id>/library/delete", methods=["POST"])
@admin_required
def admin_clear_library(user_id):
    with db() as conn:
        target = conn.execute("SELECT id, username FROM users WHERE id=?", (user_id,)).fetchone()
        if not target:
            abort(404)
        rows = conn.execute("SELECT id, filename FROM photos WHERE owner_id=?", (user_id,)).fetchall()
        conn.execute("DELETE FROM photos WHERE owner_id=?", (user_id,))
    for row in rows:
        (UPLOAD_DIR / row["filename"]).unlink(missing_ok=True)
        (THUMB_DIR / f"{row['id']}.jpg").unlink(missing_ok=True)
    log_activity("library_cleared", f"Cleared {len(rows)} photos from {target['username']}")
    flash(f"Deleted {len(rows)} photos from {target['username']}'s library.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/settings", methods=["POST"])
@admin_required
def admin_settings():
    global MAX_UPLOAD_MB
    try:
        requested = int(request.form.get("max_upload_mb", "512"))
    except ValueError:
        requested = 0
    if not 16 <= requested <= 2048:
        flash("Upload limit must be between 16 and 2048 MB.", "error")
        return redirect(url_for("admin_dashboard"))
    MAX_UPLOAD_MB = requested
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
    CONFIG["max_upload_mb"] = MAX_UPLOAD_MB
    write_config(CONFIG)
    log_activity("server_setting_changed", f"Per-request upload limit set to {MAX_UPLOAD_MB} MB")
    flash("Server settings saved. The upload limit applies immediately and persists across restarts.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/backup", methods=["POST"])
@admin_required
def admin_backup():
    # Snapshot SQLite first so the downloadable archive is internally consistent.
    backup_name = f"stillroom-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    backup_path = BACKUP_DIR / backup_name
    with db() as conn:
        snapshot_path = BACKUP_DIR / "stillroom-snapshot.db"
        dest = sqlite3.connect(snapshot_path)
        conn.backup(dest)
        dest.close()
    try:
        with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot_path, "instance/stillroom.db")
            for folder_name, folder in (("photos", UPLOAD_DIR), ("thumbnails", THUMB_DIR), ("avatars", AVATAR_DIR)):
                for file in folder.iterdir():
                    if file.is_file():
                        archive.write(file, f"{folder_name}/{file.name}")
            if CONFIG_PATH.exists():
                archive.write(CONFIG_PATH, "instance/config.json")
    finally:
        snapshot_path.unlink(missing_ok=True)
    log_activity("backup_created", f"Created backup archive {backup_name}")
    return send_file(backup_path, as_attachment=True, download_name=backup_name, mimetype="application/zip")


@app.route("/admin/backups/<path:name>")
@admin_required
def admin_download_existing_backup(name):
    safe_name = Path(name).name
    if not safe_name.startswith("stillroom-backup-") or not safe_name.endswith(".zip"):
        abort(400)
    path = BACKUP_DIR / safe_name
    if not path.is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=safe_name, mimetype="application/zip")


@app.route("/admin/backup/<path:name>/delete", methods=["POST"])
@admin_required
def admin_delete_backup(name):
    safe_name = Path(name).name
    if not safe_name.startswith("stillroom-backup-") or not safe_name.endswith(".zip"):
        abort(400)
    (BACKUP_DIR / safe_name).unlink(missing_ok=True)
    flash("Backup file removed if it existed.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/")
@logged_in
def index():
    user = current_user()
    q = request.args.get("q", "").strip()
    view = request.args.get("view", "all")
    with db() as conn:
        sql = "SELECT * FROM photos WHERE owner_id=?"
        clauses, params = [], [user["id"]]
        if view == "favorites":
            clauses.append("favorite = 1")
        if q:
            clauses.append("(title LIKE ? OR original_name LIKE ? OR taken_at LIKE ? OR uploaded_at LIKE ?)")
            params.extend([f"%{q}%"] * 4)
        if clauses:
            sql += " AND " + " AND ".join(clauses)
        sql += " ORDER BY COALESCE(taken_at, uploaded_at) DESC, uploaded_at DESC"
        photos = [photo_dict(r) for r in conn.execute(sql, params).fetchall()]
        albums = conn.execute("SELECT a.*, COUNT(ap.photo_id) AS photo_count FROM albums a LEFT JOIN album_photos ap ON ap.album_id=a.id WHERE a.owner_id=? GROUP BY a.id ORDER BY a.name COLLATE NOCASE", (user["id"],)).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM photos WHERE owner_id=?", (user["id"],)).fetchone()[0]
        favorites = conn.execute("SELECT COUNT(*) FROM photos WHERE owner_id=? AND favorite=1", (user["id"],)).fetchone()[0]
    return render_template("index.html", photos=photos, albums=albums, total=total, favorites=favorites, q=q, view=view)


@app.route("/upload", methods=["POST"])
@logged_in
def upload():
    user = current_user()
    uploads = request.files.getlist("photos")
    saved = 0
    errors = []
    for f in uploads:
        if not f or not f.filename:
            continue
        original = Path(f.filename).name
        ext = Path(original).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            errors.append(f"{original}: unsupported file type")
            continue
        photo_id = uuid.uuid4().hex
        stored_name = photo_id + ext
        target = UPLOAD_DIR / stored_name
        try:
            f.save(target)
            with Image.open(target) as source:
                image = ImageOps.exif_transpose(source)
                width, height = image.size
                taken = None
                try:
                    exif = source.getexif()
                    raw = exif.get(36867) or exif.get(306)
                    if raw: taken = str(raw).replace(":", "-", 2).replace(" ", "T")
                except Exception:
                    pass
                if image.mode not in ("RGB", "RGBA"):
                    image = image.convert("RGBA" if "transparency" in image.info else "RGB")
                image.thumbnail((480, 480))
                thumb_path = THUMB_DIR / (photo_id + ".jpg")
                if image.mode != "RGB":
                    background = Image.new("RGB", image.size, "white")
                    if image.mode == "RGBA": background.paste(image, mask=image.getchannel("A"))
                    else: background.paste(image.convert("RGB"))
                    image = background
                # Avoid the slower optimize pass during large library imports.
                image.save(thumb_path, "JPEG", quality=82)
            with db() as conn:
                conn.execute("INSERT INTO photos (id, owner_id, filename, original_name, title, uploaded_at, taken_at, width, height) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (photo_id, user["id"], stored_name, original, Path(original).stem, datetime.now().isoformat(timespec="seconds"), taken, width, height))
            saved += 1
        except (UnidentifiedImageError, OSError, ValueError):
            target.unlink(missing_ok=True)
            (THUMB_DIR / (photo_id + ".jpg")).unlink(missing_ok=True)
            errors.append(f"{original}: couldn't read this image")
    if saved:
        log_activity("photos_uploaded", f"Uploaded {saved} photo(s)", user)
    # The browser uploader sends small batches and expects JSON progress results.
    if request.headers.get("X-Stillroom-Batch") == "1":
        return jsonify({"saved": saved, "errors": errors, "received": len(uploads)}), (207 if errors else 200)
    if saved: flash(f"Uploaded {saved} photo{'s' if saved != 1 else ''}.", "success")
    for error in errors[:5]: flash(error, "error")
    return redirect(url_for("index"))


@app.route("/photo/<photo_id>")
@logged_in
def photo_detail(photo_id):
    user = current_user()
    with db() as conn: row = owns_photo(conn, photo_id, user["id"])
    return jsonify(photo_dict(row))


@app.route("/media/<photo_id>")
@logged_in
def media(photo_id):
    user = current_user()
    with db() as conn: row = owns_photo(conn, photo_id, user["id"])
    return send_from_directory(UPLOAD_DIR, row["filename"], conditional=True)


@app.route("/thumb/<photo_id>.jpg")
@logged_in
def thumbnail(photo_id):
    user = current_user()
    with db() as conn: owns_photo(conn, photo_id, user["id"])
    path = THUMB_DIR / f"{photo_id}.jpg"
    if not path.exists(): abort(404)
    return send_from_directory(THUMB_DIR, path.name, conditional=True)


@app.route("/photo/<photo_id>/favorite", methods=["POST"])
@logged_in
def favorite(photo_id):
    user = current_user()
    with db() as conn:
        row = owns_photo(conn, photo_id, user["id"])
        new_value = 0 if row["favorite"] else 1
        conn.execute("UPDATE photos SET favorite=? WHERE id=? AND owner_id=?", (new_value, photo_id, user["id"]))
    return jsonify({"favorite": bool(new_value)})


@app.route("/photo/<photo_id>/title", methods=["POST"])
@logged_in
def rename_photo(photo_id):
    user = current_user()
    title = request.form.get("title", "").strip()[:180]
    if not title: return jsonify({"error": "Title cannot be empty."}), 400
    with db() as conn:
        owns_photo(conn, photo_id, user["id"])
        conn.execute("UPDATE photos SET title=? WHERE id=? AND owner_id=?", (title, photo_id, user["id"]))
    return jsonify({"title": title})


@app.route("/photo/<photo_id>/delete", methods=["POST"])
@logged_in
def delete_photo(photo_id):
    user = current_user()
    with db() as conn:
        row = owns_photo(conn, photo_id, user["id"])
        conn.execute("DELETE FROM photos WHERE id=? AND owner_id=?", (photo_id, user["id"]))
    (UPLOAD_DIR / row["filename"]).unlink(missing_ok=True)
    (THUMB_DIR / f"{photo_id}.jpg").unlink(missing_ok=True)
    flash("Photo deleted.", "success")
    return redirect(url_for("index"))


@app.route("/albums", methods=["POST"])
@logged_in
def create_album():
    user = current_user()
    name = request.form.get("name", "").strip()[:100]
    if not name:
        flash("Give the album a name first.", "error")
    else:
        try:
            with db() as conn:
                conn.execute("INSERT INTO albums (id, owner_id, name, created_at) VALUES (?, ?, ?, ?)", (uuid.uuid4().hex, user["id"], name, datetime.now().isoformat(timespec="seconds")))
            flash(f'Album “{name}” created.', "success")
        except sqlite3.IntegrityError: flash("An album with that name already exists.", "error")
    return redirect(url_for("index"))


@app.route("/album/<album_id>")
@logged_in
def album_detail(album_id):
    user = current_user()
    with db() as conn:
        album = owns_album(conn, album_id, user["id"])
        photos = [photo_dict(r) for r in conn.execute("SELECT p.* FROM photos p JOIN album_photos ap ON ap.photo_id=p.id WHERE ap.album_id=? AND p.owner_id=? ORDER BY COALESCE(p.taken_at,p.uploaded_at) DESC", (album_id, user["id"])).fetchall()]
        all_photos = conn.execute("SELECT id, title, original_name FROM photos WHERE owner_id=? ORDER BY title COLLATE NOCASE", (user["id"],)).fetchall()
    return render_template("album.html", album=album, photos=photos, all_photos=all_photos)


@app.route("/album/<album_id>/add", methods=["POST"])
@logged_in
def album_add(album_id):
    user = current_user()
    photo_id = request.form.get("photo_id", "")
    with db() as conn:
        owns_album(conn, album_id, user["id"])
        if conn.execute("SELECT 1 FROM photos WHERE id=? AND owner_id=?", (photo_id, user["id"])).fetchone():
            conn.execute("INSERT OR IGNORE INTO album_photos (album_id, photo_id) VALUES (?, ?)", (album_id, photo_id))
    return redirect(url_for("album_detail", album_id=album_id))


@app.route("/album/<album_id>/remove/<photo_id>", methods=["POST"])
@logged_in
def album_remove(album_id, photo_id):
    user = current_user()
    with db() as conn:
        owns_album(conn, album_id, user["id"])
        owns_photo(conn, photo_id, user["id"])
        conn.execute("DELETE FROM album_photos WHERE album_id=? AND photo_id=?", (album_id, photo_id))
    return redirect(url_for("album_detail", album_id=album_id))


@app.route("/album/<album_id>/delete", methods=["POST"])
@logged_in
def album_delete(album_id):
    user = current_user()
    with db() as conn:
        owns_album(conn, album_id, user["id"])
        conn.execute("DELETE FROM albums WHERE id=? AND owner_id=?", (album_id, user["id"]))
    flash("Album deleted. Photos were kept in your library.", "success")
    return redirect(url_for("index"))


@app.errorhandler(500)
def server_error(error):
    original = getattr(error, "original_exception", None) or error
    try:
        log_activity("server_error", f"{type(original).__name__}: {original}")
    except Exception:
        pass
    return render_template("error.html"), 500


@app.errorhandler(413)
def too_large(_error):
    flash(f"That upload is too large. Current per-request limit is {MAX_UPLOAD_MB} MB.", "error")
    return redirect(url_for("index"))


if __name__ == "__main__":
    print("Stillroom is starting. Keep this window open while it runs.")
    print("Open http://127.0.0.1:5055 on this PC.")
    print("For other devices, use this PC's LAN address or its Tailscale address.")
    app.run(host="0.0.0.0", port=int(os.environ.get("STILLROOM_PORT", "5055")), debug=False)
