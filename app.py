import json
import io
import ipaddress
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

import click
from flask import Flask, g, jsonify, request, send_file, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash
from smart_features import SMART_SCHEMA, register_smart_features


BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
DATABASE = Path(os.environ.get("CAMPUS_DATABASE", INSTANCE_DIR / "campus.db"))
ROLES = {"Student", "Faculty", "Placement Officer", "Admin"}
COMPLAINT_STATUSES = {"Submitted", "In Progress", "Resolved", "Rejected"}

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "").lower() == "true",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('Student', 'Faculty', 'Placement Officer', 'Admin')),
    student_id TEXT UNIQUE,
    employee_id TEXT UNIQUE,
    department TEXT,
    section TEXT,
    year TEXT,
    subject TEXT,
    assigned_class TEXT,
    profile_json TEXT NOT NULL DEFAULT '{}',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS complaints (
    id TEXT PRIMARY KEY,
    student_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT NOT NULL,
    location TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'Medium',
    status TEXT NOT NULL DEFAULT 'Submitted',
    department TEXT NOT NULL DEFAULT 'Pending assignment',
    resolution TEXT NOT NULL DEFAULT '',
    resolved_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS complaint_attachments (
    complaint_id TEXT PRIMARY KEY REFERENCES complaints(id) ON DELETE CASCADE,
    content_type TEXT NOT NULL,
    content BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'general',
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS announcements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    audience TEXT NOT NULL DEFAULT 'All',
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL CHECK (type IN ('MCQ', 'CODING', 'VOWEL')),
    text TEXT NOT NULL,
    marks INTEGER NOT NULL CHECK (marks > 0),
    data_json TEXT NOT NULL DEFAULT '{}',
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES users(id),
    exam_type TEXT NOT NULL,
    exam_name TEXT NOT NULL,
    subject TEXT NOT NULL,
    obtained_marks REAL NOT NULL,
    total_marks REAL NOT NULL CHECK (total_marks > 0),
    entered_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS test_submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES users(id),
    answers_json TEXT NOT NULL,
    obtained_marks REAL NOT NULL,
    total_marks REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attendance_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT NOT NULL UNIQUE,
    faculty_id INTEGER NOT NULL REFERENCES users(id),
    class_name TEXT NOT NULL,
    subject TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attendance_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES attendance_sessions(id),
    student_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    UNIQUE(session_id, student_id)
);
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    industry TEXT NOT NULL DEFAULT '',
    contact_email TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS placement_drives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    package_lpa REAL,
    drive_date TEXT,
    status TEXT NOT NULL DEFAULT 'Open',
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drive_registrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    drive_id INTEGER NOT NULL REFERENCES placement_drives(id) ON DELETE CASCADE,
    student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'Registered',
    created_at TEXT NOT NULL,
    UNIQUE(drive_id, student_id)
);
CREATE TABLE IF NOT EXISTS placement_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    package_lpa REAL NOT NULL CHECK (package_lpa >= 0),
    placed_at TEXT NOT NULL,
    created_by INTEGER NOT NULL REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS condonation_requests (
    student_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'Pending',
    updated_by INTEGER REFERENCES users(id),
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_complaints_student ON complaints(student_id, created_at);
CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_attendance_student ON attendance_records(student_id, created_at);
"""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def get_db():
    if "db" not in g:
        DATABASE.parent.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(DATABASE, isolation_level=None)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "geolocation=(self), camera=(self)")
    return response


def initialize_database():
    with app.app_context():
        db = get_db()
        db.executescript(SCHEMA + SMART_SCHEMA)
        migrations = {
            "attendance_sessions": [("status", "TEXT NOT NULL DEFAULT 'Open'"), ("closed_at", "TEXT")],
            "attendance_records": [
                ("device_hash", "TEXT NOT NULL DEFAULT ''"), ("latitude", "REAL"), ("longitude", "REAL"),
                ("accuracy", "REAL"), ("ip_address", "TEXT NOT NULL DEFAULT ''"),
                ("verification_status", "TEXT NOT NULL DEFAULT 'basic'"),
            ],
        }
        for table, columns in migrations.items():
            existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
            for column, definition in columns:
                if column not in existing:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def api_error(message, status=400):
    return jsonify({"error": message}), status


def payload():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object.")
    return data


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return get_db().execute(
        "SELECT * FROM users WHERE id = ? AND active = 1", (user_id,)
    ).fetchone()


def user_json(user):
    profile = json.loads(user["profile_json"] or "{}")
    return {
        "id": user["id"],
        "name": user["name"],
        "email": user["email"],
        "role": user["role"],
        "usn": user["student_id"],
        "employeeId": user["employee_id"],
        "department": user["department"],
        "section": user["section"],
        "year": user["year"],
        "subject": user["subject"],
        "assignedClass": user["assigned_class"],
        "branch": user["department"],
        "admissionType": profile.get("admissionType"),
        "feeStructure": profile.get("feeStructure"),
        "profile": profile,
    }


def login_required(*roles):
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if user is None:
                return api_error("Please sign in to continue.", 401)
            if roles and user["role"] not in roles:
                return api_error("You do not have permission to perform this action.", 403)
            g.current_user = user
            return view(*args, **kwargs)

        return wrapped

    return decorate


def add_notification(user_id, title, message, kind="general"):
    get_db().execute(
        "INSERT INTO notifications (user_id, title, message, type, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, title, message, kind, utc_now()),
    )


def complaint_json(row):
    return {
        "id": row["id"],
        "studentEmail": row["email"],
        "studentName": row["student_name"],
        "title": row["title"],
        "category": row["category"],
        "description": row["description"],
        "location": row["location"],
        "priority": row["priority"],
        "status": row["status"],
        "date": row["created_at"],
        "department": row["department"],
        "resolution": row["resolution"],
        "resolvedBy": row["resolved_by"],
        "evidence": bool(row["has_evidence"]),
    }


COMPLAINT_SELECT = """
SELECT c.*, u.email, u.name AS student_name,
EXISTS(SELECT 1 FROM complaint_attachments a WHERE a.complaint_id = c.id) AS has_evidence
FROM complaints c JOIN users u ON u.id = c.student_id
"""


@app.errorhandler(ValueError)
def handle_value_error(error):
    return api_error(str(error), 400)


@app.errorhandler(sqlite3.IntegrityError)
def handle_integrity_error(error):
    message = str(error)
    if "UNIQUE constraint failed" in message:
        return api_error("A record with those details already exists.", 409)
    return api_error("The request conflicts with related records.", 409)


@app.get("/")
def home():
    return serve_html("index.html")


def serve_html(filename):
    response = send_from_directory(BASE_DIR, filename)
    response.direct_passthrough = False
    html = response.get_data(as_text=True)
    if '/static/js/api.js' not in html and 'src="static/js/api.js"' not in html and "src='static/js/api.js'" not in html:
        html = html.replace(
            "</head>",
            '<script src="/static/js/api.js"></script></head>',
            1,
        )
    response.set_data(html)
    return response


SAFE_WEB_EXTENSIONS = {
    ".html", ".css", ".js", ".json", ".png", ".jpg", ".jpeg", ".gif",
    ".svg", ".ico", ".webp", ".woff", ".woff2", ".ttf",
}


@app.get("/<path:filename>")
def static_page(filename):
    if filename.startswith(("api/", "instance/")) or ".." in Path(filename).parts:
        return api_error("Not found.", 404)
    path = (BASE_DIR / filename).resolve()
    if BASE_DIR not in path.parents or not path.is_file():
        return api_error("Not found.", 404)
    if path.suffix.lower() not in SAFE_WEB_EXTENSIONS:
        return api_error("Not found.", 404)
    if path.suffix.lower() == ".html":
        return serve_html(filename)
    return send_from_directory(BASE_DIR, filename)


@app.get("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/auth/login", methods=["POST", "OPTIONS"])
@app.route("/api/auth/login/", methods=["POST", "OPTIONS"])
def auth_login():
    if request.method == "OPTIONS":
        response = jsonify({"ok": True})
        response.status_code = 204
        response.headers["Access-Control-Allow-Origin"] = request.headers.get("Origin", "*")
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        return response
    data = payload()
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    role = str(data.get("role", "")).strip()
    if not email or not password:
        return api_error("Email and password are required.")
    user = get_db().execute(
        "SELECT * FROM users WHERE email = ? COLLATE NOCASE AND active = 1", (email,)
    ).fetchone()
    if user is None or not check_password_hash(user["password_hash"], password):
        return api_error("Invalid email or password.", 401)
    if role and user["role"].lower() != role.lower():
        return api_error("This account does not have access to that portal.", 403)
    if user["role"] == "Admin":
        admin_key = os.environ.get("CAMPUS_ADMIN_KEY")
        if not admin_key or not secrets.compare_digest(
            str(data.get("adminKey", "")), admin_key
        ):
            return api_error("Invalid or unconfigured administrator key.", 401)
    if user["role"] == "Faculty" and user["employee_id"]:
        if str(data.get("employeeId", "")).strip().lower() != user["employee_id"].lower():
            return api_error("Employee ID does not match this account.", 401)
    session.clear()
    session["user_id"] = user["id"]
    session.permanent = True
    return jsonify({"user": user_json(user)})


@app.post("/api/login")
def auth_login_legacy():
    """Compatibility endpoint for older login pages; uses the same secure session login."""
    return auth_login()


@app.post("/api/auth/logout")
def auth_logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/auth/me")
@login_required()
def auth_me():
    return jsonify({"user": user_json(g.current_user)})


@app.post("/api/auth/password")
@login_required()
def change_password():
    data = payload()
    old_password = str(data.get("currentPassword", ""))
    new_password = str(data.get("newPassword", ""))
    if not check_password_hash(g.current_user["password_hash"], old_password):
        return api_error("Current password is incorrect.", 401)
    if len(new_password) < 10:
        return api_error("New password must be at least 10 characters long.")
    get_db().execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(new_password), g.current_user["id"]),
    )
    return jsonify({"ok": True})


@app.get("/api/profile")
@login_required()
def get_profile():
    return jsonify({"user": user_json(g.current_user)})


@app.patch("/api/profile")
@login_required()
def update_profile():
    data = payload()
    name = str(data.get("name", g.current_user["name"])).strip()
    if not name or len(name) > 120:
        return api_error("Name must contain between 1 and 120 characters.")
    profile = json.loads(g.current_user["profile_json"] or "{}")
    updates = data.get("profile", {})
    if not isinstance(updates, dict):
        return api_error("Profile details must be an object.")
    profile.update(updates)
    get_db().execute(
        "UPDATE users SET name = ?, profile_json = ? WHERE id = ?",
        (name, json.dumps(profile), g.current_user["id"]),
    )
    user = current_user()
    return jsonify({"user": user_json(user)})


@app.get("/api/users")
@login_required("Admin")
def list_users():
    rows = get_db().execute(
        "SELECT * FROM users WHERE active = 1 ORDER BY role, name"
    ).fetchall()
    return jsonify({"users": [user_json(row) for row in rows]})


@app.post("/api/users")
@login_required("Admin")
def create_user():
    data = payload()
    name = str(data.get("name", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    role = str(data.get("role", ""))
    if not name or not email or "@" not in email:
        return api_error("A valid name and email are required.")
    if role not in {"Student", "Faculty", "Placement Officer"}:
        return api_error("Choose Student, Faculty, or Placement Officer.")
    if len(password) < 10:
        return api_error("Password must be at least 10 characters long.")
    student_id = str(data.get("usn", "")).strip() or None
    employee_id = str(data.get("employeeId", "")).strip() or None
    if role == "Student" and not student_id:
        return api_error("Student ID / USN is required.")
    if role == "Faculty" and not employee_id:
        return api_error("Employee ID is required.")
    profile = data.get("profile", {})
    if not isinstance(profile, dict):
        return api_error("Additional account details must be an object.")
    cursor = get_db().execute(
        """INSERT INTO users
        (name, email, password_hash, role, student_id, employee_id, department,
         section, year, subject, assigned_class, profile_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            name, email, generate_password_hash(password), role, student_id, employee_id,
            str(data.get("department", "")).strip() or None,
            str(data.get("section", "")).strip() or None,
            str(data.get("year", "")).strip() or None,
            str(data.get("subject", "")).strip() or None,
            str(data.get("assignedClass", "")).strip() or None,
            json.dumps(profile),
            utc_now(),
        ),
    )
    row = get_db().execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return jsonify({"user": user_json(row)}), 201


@app.delete("/api/users/<int:user_id>")
@login_required("Admin")
def delete_user(user_id):
    if user_id == g.current_user["id"]:
        return api_error("You cannot deactivate your own account.")
    cursor = get_db().execute("UPDATE users SET active = 0 WHERE id = ?", (user_id,))
    if cursor.rowcount == 0:
        return api_error("User not found.", 404)
    return jsonify({"ok": True})


@app.get("/api/complaints")
@login_required()
def list_complaints():
    db = get_db()
    if g.current_user["role"] == "Student":
        rows = db.execute(
            COMPLAINT_SELECT + " WHERE c.student_id = ? ORDER BY c.created_at DESC",
            (g.current_user["id"],),
        ).fetchall()
    elif g.current_user["role"] == "Faculty":
        rows = db.execute(COMPLAINT_SELECT + " ORDER BY c.created_at DESC").fetchall()
    elif g.current_user["role"] in {"Admin", "Placement Officer"}:
        rows = db.execute(COMPLAINT_SELECT + " ORDER BY c.created_at DESC").fetchall()
    return jsonify({"complaints": [complaint_json(row) for row in rows]})


@app.post("/api/complaints")
@login_required("Student")
def create_complaint():
    data = payload() if request.is_json else request.form.to_dict()
    fields = {
        key: str(data.get(key, "")).strip()
        for key in ("title", "category", "description", "location")
    }
    if any(not value for value in fields.values()):
        return api_error("Title, category, description, and location are required.")
    if len(fields["title"]) > 160 or len(fields["description"]) > 5000:
        return api_error("Complaint title or description is too long.")
    priority = str(data.get("priority", "Medium"))
    if priority not in {"Low", "Medium", "High"}:
        return api_error("Choose Low, Medium, or High priority.")
    requested_date = str(data.get("date", "")).strip()
    created_at = utc_now()
    if requested_date:
        try:
            parsed_date = datetime.fromisoformat(requested_date)
        except ValueError:
            return api_error("Enter a valid complaint date.")
        if parsed_date.tzinfo is None:
            parsed_date = parsed_date.replace(tzinfo=timezone.utc)
        created_at = parsed_date.astimezone(timezone.utc).isoformat()
    attachment_content = None
    attachment_type = None
    attachment = request.files.get("evidence")
    if attachment and attachment.filename:
        attachment_type = attachment.mimetype
        signatures = {
            "image/png": b"\x89PNG\r\n\x1a\n",
            "image/jpeg": b"\xff\xd8\xff",
            "image/gif": (b"GIF87a", b"GIF89a"),
            "image/webp": b"RIFF",
        }
        attachment_content = attachment.read(1024 * 1024 + 1)
        if len(attachment_content) > 1024 * 1024:
            return api_error("Evidence images must be 1 MB or smaller.")
        signature = signatures.get(attachment_type)
        if signature is None or not (
            any(attachment_content.startswith(value) for value in signature)
            if isinstance(signature, tuple) else attachment_content.startswith(signature)
        ):
            return api_error("Attach a valid PNG, JPEG, GIF, or WebP image.")
        if attachment_type == "image/webp" and attachment_content[8:12] != b"WEBP":
            return api_error("Attach a valid WebP image.")
    complaint_id = "CMP-" + secrets.token_hex(4).upper()
    now = utc_now()
    db = get_db()
    db.execute(
        """INSERT INTO complaints
        (id, student_id, title, category, description, location, priority, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            complaint_id, g.current_user["id"], fields["title"], fields["category"],
            fields["description"], fields["location"],
            priority, created_at, now,
        ),
    )
    if attachment_content is not None:
        db.execute(
            """INSERT INTO complaint_attachments (complaint_id, content_type, content)
            VALUES (?, ?, ?)""",
            (complaint_id, attachment_type, attachment_content),
        )
    db.execute("INSERT INTO complaint_updates (complaint_id, status, note, updated_by, created_at) VALUES (?, 'Submitted', 'Complaint submitted by student.', ?, ?)", (complaint_id, g.current_user["id"], now))
    add_notification(
        g.current_user["id"], f"Complaint {complaint_id} submitted.",
        f'Your complaint "{fields["title"]}" has been submitted.', "complaint",
    )
    staff = db.execute("SELECT id FROM users WHERE role = 'Faculty' AND active = 1").fetchall()
    for row in staff:
        add_notification(
            row["id"], f"New complaint {complaint_id}",
            f'{g.current_user["name"]} submitted: "{fields["title"]}".', "new_complaint",
        )
    row = db.execute(
        COMPLAINT_SELECT + " WHERE c.id = ?", (complaint_id,)
    ).fetchone()
    return jsonify({"complaint": complaint_json(row)}), 201


@app.get("/api/complaints/<complaint_id>/evidence")
@login_required()
def get_complaint_evidence(complaint_id):
    row = get_db().execute(
        """SELECT a.content, a.content_type, c.student_id
        FROM complaint_attachments a JOIN complaints c ON c.id = a.complaint_id
        WHERE a.complaint_id = ?""",
        (complaint_id,),
    ).fetchone()
    if row is None:
        return api_error("Evidence not found.", 404)
    if g.current_user["role"] == "Student" and row["student_id"] != g.current_user["id"]:
        return api_error("You do not have permission to access this evidence.", 403)
    if g.current_user["role"] not in {"Student", "Faculty", "Admin"}:
        return api_error("You do not have permission to access this evidence.", 403)
    extension = row["content_type"].split("/")[-1]
    if extension == "jpeg":
        extension = "jpg"
    return send_file(
        io.BytesIO(row["content"]),
        mimetype=row["content_type"],
        as_attachment=True,
        download_name=f"complaint-{complaint_id}.{extension}",
    )


@app.patch("/api/complaints/<complaint_id>")
@login_required("Faculty", "Admin")
def update_complaint(complaint_id):
    data = payload()
    db = get_db()
    row = db.execute(
        COMPLAINT_SELECT + " WHERE c.id = ?", (complaint_id,)
    ).fetchone()
    if row is None:
        return api_error("Complaint not found.", 404)
    status = str(data.get("status", row["status"]))
    if status not in COMPLAINT_STATUSES:
        return api_error("Choose a valid complaint status.")
    department = str(data.get("department", row["department"])).strip()
    resolution = str(data.get("resolution", row["resolution"])).strip()
    db.execute(
        """UPDATE complaints SET status = ?, department = ?, resolution = ?,
        resolved_by = ?, updated_at = ? WHERE id = ?""",
        (
            status, department, resolution,
            g.current_user["name"] if status == "Resolved" else "",
            utc_now(), complaint_id,
        ),
    )
    db.execute("INSERT INTO complaint_updates (complaint_id, status, note, updated_by, created_at) VALUES (?, ?, ?, ?, ?)", (complaint_id, status, resolution or "Status updated.", g.current_user["id"], utc_now()))
    add_notification(
        row["student_id"], "Complaint updated",
        f'Your complaint "{row["title"]}" status changed to {status}. {resolution}'.strip(),
        "complaint",
    )
    updated = db.execute(COMPLAINT_SELECT + " WHERE c.id = ?", (complaint_id,)).fetchone()
    return jsonify({"complaint": complaint_json(updated)})


@app.get("/api/announcements")
@login_required()
def list_announcements():
    rows = get_db().execute(
        """SELECT a.*, u.name AS author FROM announcements a
        JOIN users u ON u.id = a.created_by
        WHERE a.audience = 'All' OR a.audience = ?
        ORDER BY a.created_at DESC""",
        (g.current_user["role"],),
    ).fetchall()
    return jsonify({"announcements": [
        {"id": row["id"], "title": row["title"], "message": row["message"],
         "audience": row["audience"], "author": row["author"], "date": row["created_at"]}
        for row in rows
    ]})


@app.post("/api/announcements")
@login_required("Admin", "Faculty", "Placement Officer")
def create_announcement():
    data = payload()
    title = str(data.get("title", "")).strip()
    message = str(data.get("message", "")).strip()
    audience = str(data.get("audience", "All")).strip()
    if not title or not message:
        return api_error("Announcement title and message are required.")
    if audience not in {"All", "Student", "Faculty", "Placement Officer"}:
        return api_error("Choose a valid announcement audience.")
    cursor = get_db().execute(
        """INSERT INTO announcements (title, message, audience, created_by, created_at)
        VALUES (?, ?, ?, ?, ?)""",
        (title, message, audience, g.current_user["id"], utc_now()),
    )
    row = get_db().execute(
        """SELECT a.*, u.name AS author FROM announcements a JOIN users u ON u.id = a.created_by
        WHERE a.id = ?""", (cursor.lastrowid,)
    ).fetchone()
    recipients = get_db().execute(
        "SELECT id FROM users WHERE active = 1 AND (? = 'All' OR role = ?)",
        (audience, audience),
    ).fetchall()
    for recipient in recipients:
        add_notification(
            recipient["id"], title, message, "general"
        )
    return jsonify({"announcement": {
        "id": row["id"], "title": row["title"], "message": row["message"],
        "audience": row["audience"], "author": row["author"], "date": row["created_at"],
    }}), 201


@app.delete("/api/announcements/<int:announcement_id>")
@login_required("Admin")
def delete_announcement(announcement_id):
    cursor = get_db().execute("DELETE FROM announcements WHERE id = ?", (announcement_id,))
    if cursor.rowcount == 0:
        return api_error("Announcement not found.", 404)
    return jsonify({"ok": True})


@app.get("/api/notifications")
@login_required()
def list_notifications():
    rows = get_db().execute(
        "SELECT * FROM notifications WHERE user_id = ? ORDER BY created_at DESC",
        (g.current_user["id"],),
    ).fetchall()
    return jsonify({"notifications": [
        {"id": row["id"], "title": row["title"], "message": row["message"],
         "type": row["type"], "read": bool(row["is_read"]), "timestamp": row["created_at"]}
        for row in rows
    ]})


@app.patch("/api/notifications/read")
@login_required()
def mark_notifications_read():
    data = payload()
    notification_id = data.get("id")
    if notification_id is None:
        get_db().execute(
            "UPDATE notifications SET is_read = 1 WHERE user_id = ?",
            (g.current_user["id"],),
        )
    else:
        get_db().execute(
            "UPDATE notifications SET is_read = 1 WHERE id = ? AND user_id = ?",
            (notification_id, g.current_user["id"]),
        )
    return jsonify({"ok": True})


@app.delete("/api/notifications")
@login_required()
def clear_notifications():
    get_db().execute("DELETE FROM notifications WHERE user_id = ?", (g.current_user["id"],))
    return jsonify({"ok": True})


@app.get("/api/questions")
@login_required()
def list_questions():
    rows = get_db().execute("SELECT * FROM questions ORDER BY created_at DESC").fetchall()
    questions = []
    for row in rows:
        question = {
            "id": row["id"], "type": row["type"], "text": row["text"],
            "marks": row["marks"], "timestamp": row["created_at"],
        }
        question.update(json.loads(row["data_json"]))
        if g.current_user["role"] == "Student":
            question.pop("correctAnswer", None)
            question.pop("expectedAnswer", None)
        questions.append(question)
    return jsonify({"questions": questions})


@app.post("/api/questions")
@login_required("Faculty", "Admin")
def create_question():
    data = payload()
    kind = str(data.get("type", ""))
    text = str(data.get("text", "")).strip()
    try:
        marks = int(data.get("marks", 0))
    except (TypeError, ValueError):
        return api_error("Marks must be a positive whole number.")
    if kind not in {"MCQ", "CODING", "VOWEL"} or not text or marks < 1:
        return api_error("A valid question type, text, and positive marks are required.")
    extra = {key: value for key, value in data.items()
             if key not in {"type", "text", "marks", "id", "timestamp"}}
    if kind == "MCQ":
        options = extra.get("options")
        if not isinstance(options, dict) or any(
            not str(options.get(letter, "")).strip() for letter in ("A", "B", "C", "D")
        ) or extra.get("correctAnswer") not in {"A", "B", "C", "D"}:
            return api_error("MCQ questions need four options and a valid correct answer.")
    elif kind == "VOWEL" and (
        str(extra.get("inputChar", "")).strip() == ""
        or extra.get("expectedAnswer") not in {"Vowel", "Consonant"}
    ):
        return api_error("Vowel questions need a character and valid expected answer.")
    elif kind == "CODING" and not str(extra.get("expectedOutput", "")).strip():
        return api_error("Coding questions need an expected output description.")
    cursor = get_db().execute(
        """INSERT INTO questions (type, text, marks, data_json, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?)""",
        (kind, text, marks, json.dumps(extra), g.current_user["id"], utc_now()),
    )
    return jsonify({"id": cursor.lastrowid}), 201


@app.delete("/api/questions/<int:question_id>")
@login_required("Faculty", "Admin")
def delete_question(question_id):
    cursor = get_db().execute("DELETE FROM questions WHERE id = ?", (question_id,))
    if cursor.rowcount == 0:
        return api_error("Question not found.", 404)
    return jsonify({"ok": True})


@app.post("/api/tests/submit")
@login_required("Student")
def submit_test():
    data = payload()
    answers = data.get("answers", {})
    if not isinstance(answers, dict):
        return api_error("Answers must be an object.")
    questions = get_db().execute("SELECT * FROM questions").fetchall()
    total = 0
    obtained = 0
    correct = 0
    for row in questions:
        question = {"type": row["type"]}
        question.update(json.loads(row["data_json"]))
        total += row["marks"]
        answer = answers.get(str(row["id"]), answers.get(row["id"]))
        if row["type"] == "MCQ" and answer == question.get("correctAnswer"):
            obtained += row["marks"]
            correct += 1
        elif row["type"] == "VOWEL" and answer == question.get("expectedAnswer"):
            obtained += row["marks"]
            correct += 1
        elif row["type"] == "CODING" and isinstance(answer, str) and len(answer.strip()) > 10:
            obtained += row["marks"] * 0.5
    if total <= 0:
        return api_error("There are no questions available for this test.", 409)
    now = utc_now()
    get_db().execute(
        """INSERT INTO test_submissions
        (student_id, answers_json, obtained_marks, total_marks, created_at)
        VALUES (?, ?, ?, ?, ?)""",
        (g.current_user["id"], json.dumps(answers), obtained, total, now),
    )
    return jsonify({
        "obtainedMarks": obtained, "totalMarks": total,
        "percentage": round(obtained / total * 100, 2), "timestamp": now,
        "correct": correct, "attempted": len(answers), "totalQuestions": len(questions),
    }), 201


@app.get("/api/results")
@login_required()
def list_results():
    if g.current_user["role"] == "Student":
        rows = get_db().execute(
            """SELECT r.*, u.email, u.name FROM results r
            JOIN users u ON u.id = r.student_id WHERE r.student_id = ?
            UNION ALL
            SELECT t.id, t.student_id, 'Online Test', 'Online Test', 'Online assessment',
            t.obtained_marks, t.total_marks, t.student_id, t.created_at, u.email, u.name
            FROM test_submissions t JOIN users u ON u.id = t.student_id
            WHERE t.student_id = ? ORDER BY created_at DESC""",
            (g.current_user["id"], g.current_user["id"]),
        ).fetchall()
    elif g.current_user["role"] in {"Faculty", "Admin"}:
        rows = get_db().execute(
            """SELECT r.*, u.email, u.name FROM results r JOIN users u ON u.id = r.student_id
            ORDER BY r.created_at DESC"""
        ).fetchall()
    else:
        return jsonify({"results": []})
    return jsonify({"results": [
        {"id": row["id"], "studentEmail": row["email"], "studentName": row["name"],
         "examType": row["exam_type"], "examName": row["exam_name"],
         "subject": row["subject"], "obtainedMarks": row["obtained_marks"],
         "totalMarks": row["total_marks"],
         "percentage": round(row["obtained_marks"] / row["total_marks"] * 100, 2),
         "timestamp": row["created_at"]}
        for row in rows
    ]})


@app.post("/api/results")
@login_required("Faculty", "Admin")
def create_result():
    data = payload()
    email = str(data.get("studentEmail", "")).strip().lower()
    try:
        obtained = float(data.get("obtainedMarks"))
        total = float(data.get("totalMarks"))
    except (TypeError, ValueError):
        return api_error("Marks must be numeric.")
    if total <= 0 or obtained < 0 or obtained > total:
        return api_error("Marks must be non-negative and obtained marks cannot exceed total marks.")
    student = get_db().execute(
        "SELECT id FROM users WHERE email = ? AND role = 'Student' AND active = 1",
        (email,),
    ).fetchone()
    if student is None:
        return api_error("No active student account exists for that email.", 404)
    cursor = get_db().execute(
        """INSERT INTO results
        (student_id, exam_type, exam_name, subject, obtained_marks, total_marks, entered_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            student["id"], str(data.get("examType", "Assessment")),
            str(data.get("examName", "")).strip(), str(data.get("subject", "")).strip(),
            obtained, total, g.current_user["id"], utc_now(),
        ),
    )
    add_notification(
        student["id"], "Academic result published",
        f'{str(data.get("examName", "")).strip() or "Assessment"} result is available.',
        "result",
    )
    return jsonify({"id": cursor.lastrowid}), 201


@app.delete("/api/results/<int:result_id>")
@login_required("Faculty", "Admin")
def delete_result(result_id):
    db = get_db()
    result = db.execute(
        "SELECT entered_by FROM results WHERE id = ?", (result_id,)
    ).fetchone()
    if result is None:
        return api_error("Result not found.", 404)
    if g.current_user["role"] == "Faculty" and result["entered_by"] != g.current_user["id"]:
        return api_error("You can only delete results you entered.", 403)
    db.execute("DELETE FROM results WHERE id = ?", (result_id,))
    return jsonify({"ok": True})


@app.post("/api/attendance/sessions")
@login_required("Faculty", "Admin")
def create_attendance_session():
    data = payload()
    class_name = str(data.get("class", "")).strip()
    subject = str(data.get("subject", "")).strip()
    if not class_name or not subject:
        return api_error("Class and subject are required.")
    faculty_class = str(g.current_user["assigned_class"] or "").strip()
    faculty_subject = str(g.current_user["subject"] or "").strip()
    if faculty_class and faculty_class.casefold() != class_name.casefold():
        return api_error("You can only create attendance sessions for your assigned class.", 403)
    if faculty_subject and faculty_subject.casefold() != subject.casefold():
        return api_error("You can only create attendance sessions for your assigned subject.", 403)
    token = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=5)
    get_db().execute(
        """INSERT INTO attendance_sessions
        (token, faculty_id, class_name, subject, expires_at, created_at, status)
        VALUES (?, ?, ?, ?, ?, ?, 'Open')""",
        (token, g.current_user["id"], class_name, subject, expires.isoformat(), now.isoformat()),
    )
    return jsonify({
        "token": token, "class": class_name, "subject": subject,
        "date": now.date().isoformat(), "time": now.strftime("%H:%M:%S"),
        "expiresAt": expires.isoformat(),
    }), 201


@app.get("/api/attendance/sessions")
@login_required("Faculty", "Admin")
def list_attendance_sessions():
    db = get_db()
    query = """SELECT s.*,
        COUNT(ar.id) AS present,
        (SELECT COUNT(*) FROM users u WHERE u.role = 'Student' AND u.active = 1
         AND lower(COALESCE(u.department, '') || '-' || COALESCE(u.section, '')) = lower(s.class_name)
        ) AS enrolled
        FROM attendance_sessions s LEFT JOIN attendance_records ar ON ar.session_id = s.id"""
    parameters = ()
    if g.current_user["role"] == "Faculty":
        query += " WHERE s.faculty_id = ?"
        parameters = (g.current_user["id"],)
    query += " GROUP BY s.id ORDER BY s.created_at DESC"
    rows = db.execute(query, parameters).fetchall()
    return jsonify({"sessions": [
        {"id": row["id"], "token": row["token"], "class": row["class_name"],
         "subject": row["subject"], "date": row["created_at"][:10],
         "time": row["created_at"][11:19], "expiresAt": row["expires_at"],
         "status": row["status"] if "status" in row.keys() else "Open",
         "present": row["present"], "totalStudents": row["enrolled"]}
        for row in rows
    ]})


@app.get("/api/attendance/sessions/<token>")
@login_required("Faculty", "Admin")
def get_attendance_session(token):
    query = """SELECT s.*,
        COUNT(ar.id) AS present,
        (SELECT COUNT(*) FROM users u WHERE u.role = 'Student' AND u.active = 1
         AND lower(COALESCE(u.department, '') || '-' || COALESCE(u.section, '')) = lower(s.class_name)
        ) AS enrolled
        FROM attendance_sessions s LEFT JOIN attendance_records ar ON ar.session_id = s.id
        WHERE s.token = ?"""
    parameters = [token]
    if g.current_user["role"] == "Faculty":
        query += " AND s.faculty_id = ?"
        parameters.append(g.current_user["id"])
    row = get_db().execute(query + " GROUP BY s.id", parameters).fetchone()
    if row is None:
        return api_error("Attendance session not found.", 404)
    return jsonify({
        "session": {"class": row["class_name"], "subject": row["subject"],
                    "present": row["present"], "totalStudents": row["enrolled"]}
    })


@app.post("/api/attendance/scan")
@login_required("Student")
def scan_attendance():
    data = payload()
    token = str(data.get("token", "")).strip()
    db = get_db()
    row = db.execute("SELECT * FROM attendance_sessions WHERE token = ?", (token,)).fetchone()
    if row is None:
        return api_error("Attendance QR code is invalid or not found.", 404)
    if "status" in row.keys() and row["status"] != "Open":
        return api_error("This QR session is closed.", 410)
    if row["expires_at"] <= utc_now():
        return api_error("QR Code Expired. Ask faculty to generate a new QR.", 410)
    student_class = "{}-{}".format(g.current_user["department"] or "", g.current_user["section"] or "").strip("-")
    if student_class and student_class.casefold() != row["class_name"].casefold():
        return api_error("This attendance session is not for your assigned class.", 403)
    existing = db.execute("SELECT 1 FROM attendance_records WHERE session_id = ? AND student_id = ?", (row["id"], g.current_user["id"])).fetchone()
    if existing:
        return jsonify({"ok": True, "status": "Already Marked", "message": "Attendance was already marked for this session."})
    device_hash = str(data.get("deviceHash", "")).strip()[:128]
    if device_hash:
        other = db.execute("SELECT student_id FROM attendance_records WHERE session_id=? AND device_hash=? AND student_id<>?", (row["id"], device_hash, g.current_user["id"])).fetchone()
        if other:
            return api_error("This device has already been used for another student in this session.", 409)
    allowed = os.environ.get("CAMPUS_ALLOWED_NETWORKS", "").strip()
    if allowed:
        try:
            ip = ipaddress.ip_address(request.remote_addr or "")
            if not any(ip in ipaddress.ip_network(x.strip(), strict=False) for x in allowed.split(",") if x.strip()):
                return api_error("Connect to the configured college network to mark attendance.", 403)
        except ValueError:
            return api_error("Unable to verify your network connection.", 403)
    latitude, longitude, accuracy = data.get("latitude"), data.get("longitude"), data.get("accuracy")
    verification = "gps+device" if latitude is not None and longitude is not None else "device" if device_hash else "basic"
    db.execute("INSERT INTO attendance_records (session_id, student_id, created_at, device_hash, latitude, longitude, accuracy, ip_address, verification_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (row["id"], g.current_user["id"], utc_now(), device_hash, latitude, longitude, accuracy, request.remote_addr or "", verification))
    if device_hash:
        db.execute("INSERT INTO attendance_devices (session_id, student_id, device_hash, first_seen_at) VALUES (?, ?, ?, ?)", (row["id"], g.current_user["id"], device_hash, utc_now()))
    add_notification(g.current_user["id"], "Attendance marked", f'Attendance marked for {row["subject"]} ({row["class_name"]}).', "attendance")
    return jsonify({"ok": True, "status": "Present", "class": row["class_name"], "subject": row["subject"], "verification": verification}), 201


@app.get("/api/attendance")
@login_required()
def list_attendance():
    db = get_db()
    if g.current_user["role"] == "Student":
        records = db.execute(
            """SELECT ar.created_at, s.class_name, s.subject FROM attendance_records ar
            JOIN attendance_sessions s ON s.id = ar.session_id
            WHERE ar.student_id = ? ORDER BY ar.created_at DESC""",
            (g.current_user["id"],),
        ).fetchall()
        class_name = "{}-{}".format(
            g.current_user["department"] or "", g.current_user["section"] or ""
        ).strip("-")
        total_sessions = db.execute(
            """SELECT COUNT(*) AS count FROM attendance_sessions
            WHERE lower(class_name) = lower(?)""",
            (class_name,),
        ).fetchone()["count"]
        return jsonify({"records": [
            {"date": row["created_at"][:10], "time": row["created_at"][11:19],
             "class": row["class_name"], "subject": row["subject"], "status": "Present"}
            for row in records
        ], "totalSessions": total_sessions})
    if g.current_user["role"] not in {"Faculty", "Admin"}:
        return jsonify({"records": []})
    query = """SELECT ar.created_at, s.class_name, s.subject, u.name, u.student_id, u.email,
        ar.session_id AS session_id
        FROM attendance_records ar JOIN attendance_sessions s ON s.id = ar.session_id
        JOIN users u ON u.id = ar.student_id"""
    parameters = ()
    if g.current_user["role"] == "Faculty":
        query += " WHERE s.faculty_id = ?"
        parameters = (g.current_user["id"],)
    records = db.execute(query + " ORDER BY ar.created_at DESC", parameters).fetchall()
    return jsonify({"records": [
        {"date": row["created_at"][:10], "time": row["created_at"][11:19],
         "class": row["class_name"], "subject": row["subject"],
         "studentName": row["name"], "usn": row["student_id"], "email": row["email"],
         "sessionId": row["session_id"]}
        for row in records
    ]})


@app.get("/api/attendance/summary")
@login_required("Admin")
def attendance_summary():
    rows = get_db().execute(
        """SELECT u.id, u.student_id AS usn, u.name,
        (SELECT COUNT(*) FROM attendance_sessions s
         WHERE lower(s.class_name) = lower(COALESCE(u.department, '') || '-' || COALESCE(u.section, ''))
        ) AS total_sessions,
        (SELECT COUNT(DISTINCT ar.session_id) FROM attendance_records ar
         WHERE ar.student_id = u.id) AS present,
        COALESCE(cr.status, 'Pending') AS condonation
        FROM users u
        LEFT JOIN condonation_requests cr ON cr.student_id = u.id
        WHERE u.role = 'Student' AND u.active = 1
        GROUP BY u.id ORDER BY u.name"""
    ).fetchall()
    return jsonify({"students": [
        {"id": row["id"], "usn": row["usn"], "name": row["name"],
         "present": row["present"], "totalSessions": row["total_sessions"],
         "percentage": round(row["present"] / row["total_sessions"] * 100) if row["total_sessions"] else 0,
         "condonation": row["condonation"]}
        for row in rows
    ]})


@app.patch("/api/attendance/condonation/<student_id>")
@login_required("Admin")
def update_condonation(student_id):
    data = payload()
    status = str(data.get("status", ""))
    if status not in {"Approved", "Rejected", "Pending"}:
        return api_error("Choose Approved, Rejected, or Pending.")
    student = get_db().execute(
        "SELECT id FROM users WHERE student_id = ? AND role = 'Student' AND active = 1",
        (student_id,),
    ).fetchone()
    if student is None:
        return api_error("Student account not found.", 404)
    get_db().execute(
        """INSERT INTO condonation_requests (student_id, status, updated_by, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(student_id) DO UPDATE SET status = excluded.status,
        updated_by = excluded.updated_by, updated_at = excluded.updated_at""",
        (student["id"], status, g.current_user["id"], utc_now()),
    )
    return jsonify({"ok": True})


@app.get("/api/settings")
@login_required("Admin")
def get_settings():
    rows = get_db().execute("SELECT key, value FROM settings").fetchall()
    return jsonify({"settings": {row["key"]: row["value"] for row in rows}})


@app.put("/api/settings")
@login_required("Admin")
def update_settings():
    data = payload()
    portal_name = str(data.get("portalName", "")).strip()
    if not portal_name or len(portal_name) > 80:
        return api_error("Portal name must contain 1 to 80 characters.")
    get_db().execute(
        """INSERT INTO settings (key, value) VALUES ('portalName', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (portal_name,),
    )
    return jsonify({"portalName": portal_name})


@app.get("/api/placements")
@login_required()
def list_placements():
    db = get_db()
    drives = db.execute(
        """SELECT d.*, c.name AS company, c.industry,
        (SELECT COUNT(*) FROM drive_registrations r WHERE r.drive_id = d.id) AS registrations
        FROM placement_drives d JOIN companies c ON c.id = d.company_id
        ORDER BY d.created_at DESC"""
    ).fetchall()
    registrations = []
    if g.current_user["role"] == "Student":
        registrations = [row["drive_id"] for row in db.execute(
            "SELECT drive_id FROM drive_registrations WHERE student_id = ?",
            (g.current_user["id"],),
        ).fetchall()]
    placed_count = db.execute("SELECT COUNT(*) AS count FROM placement_records").fetchone()["count"]
    company_count = db.execute("SELECT COUNT(*) AS count FROM companies").fetchone()["count"]
    return jsonify({
        "drives": [{
            "id": row["id"], "title": row["title"], "company": row["company"],
            "industry": row["industry"], "description": row["description"],
            "packageLpa": row["package_lpa"], "driveDate": row["drive_date"],
            "status": row["status"], "registrations": row["registrations"],
            "registered": row["id"] in registrations,
        } for row in drives],
        "companies": company_count, "placedStudents": placed_count,
    })


@app.get("/api/placements/companies")
@login_required("Placement Officer", "Admin")
def list_companies():
    rows = get_db().execute("SELECT * FROM companies ORDER BY name").fetchall()
    return jsonify({"companies": [
        {"id": row["id"], "name": row["name"], "industry": row["industry"],
         "contactEmail": row["contact_email"]}
        for row in rows
    ]})


@app.get("/api/placements/students")
@login_required("Placement Officer", "Admin")
def list_placement_students():
    rows = get_db().execute(
        """SELECT id, name, student_id FROM users
        WHERE role = 'Student' AND active = 1 ORDER BY name"""
    ).fetchall()
    return jsonify({"students": [
        {"id": row["id"], "name": row["name"], "usn": row["student_id"]}
        for row in rows
    ]})


@app.post("/api/placements/companies")
@login_required("Placement Officer", "Admin")
def create_company():
    data = payload()
    name = str(data.get("name", "")).strip()
    if not name:
        return api_error("Company name is required.")
    cursor = get_db().execute(
        "INSERT INTO companies (name, industry, contact_email, created_at) VALUES (?, ?, ?, ?)",
        (name, str(data.get("industry", "")).strip(),
         str(data.get("contactEmail", "")).strip(), utc_now()),
    )
    return jsonify({"id": cursor.lastrowid, "name": name}), 201


@app.post("/api/placements/drives")
@login_required("Placement Officer", "Admin")
def create_drive():
    data = payload()
    try:
        company_id = int(data.get("companyId"))
        package = float(data.get("packageLpa")) if data.get("packageLpa") else None
    except (TypeError, ValueError):
        return api_error("Select a company and enter a valid package amount.")
    title = str(data.get("title", "")).strip()
    if not title or (package is not None and package < 0):
        return api_error("Drive title and a non-negative package are required.")
    cursor = get_db().execute(
        """INSERT INTO placement_drives
        (company_id, title, description, package_lpa, drive_date, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            company_id, title, str(data.get("description", "")).strip(), package,
            str(data.get("driveDate", "")).strip() or None,
            g.current_user["id"], utc_now(),
        ),
    )
    drive = get_db().execute(
        "SELECT title FROM placement_drives WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()
    students = get_db().execute(
        "SELECT id FROM users WHERE role = 'Student' AND active = 1"
    ).fetchall()
    for student in students:
        add_notification(
            student["id"], "New placement drive",
            f'{drive["title"]} is now open for registration.', "company",
        )
    return jsonify({"id": cursor.lastrowid}), 201


@app.post("/api/placements/records")
@login_required("Placement Officer", "Admin")
def create_placement_record():
    data = payload()
    try:
        student_id = int(data.get("studentId"))
        company_id = int(data.get("companyId"))
        package = float(data.get("packageLpa"))
    except (TypeError, ValueError):
        return api_error("Select a student and company and enter a valid package.")
    if package < 0:
        return api_error("Package cannot be negative.")
    student = get_db().execute(
        "SELECT id FROM users WHERE id = ? AND role = 'Student' AND active = 1",
        (student_id,),
    ).fetchone()
    if student is None:
        return api_error("Active student account not found.", 404)
    cursor = get_db().execute(
        """INSERT INTO placement_records
        (student_id, company_id, package_lpa, placed_at, created_by)
        VALUES (?, ?, ?, ?, ?)""",
        (student_id, company_id, package, utc_now(), g.current_user["id"]),
    )
    add_notification(
        student_id, "Placement record updated",
        "Your placement record has been added to the portal.", "company",
    )
    return jsonify({"id": cursor.lastrowid}), 201


@app.post("/api/placements/drives/<int:drive_id>/register")
@login_required("Student")
def register_for_drive(drive_id):
    drive = get_db().execute(
        "SELECT id, status FROM placement_drives WHERE id = ?", (drive_id,)
    ).fetchone()
    if drive is None:
        return api_error("Placement drive not found.", 404)
    if drive["status"] != "Open":
        return api_error("This placement drive is not open for registration.", 409)
    get_db().execute(
        """INSERT INTO drive_registrations (drive_id, student_id, created_at)
        VALUES (?, ?, ?)""",
        (drive_id, g.current_user["id"], utc_now()),
    )
    add_notification(
        g.current_user["id"], "Placement drive registration",
        "You registered for this placement drive.", "company",
    )
    return jsonify({"ok": True}), 201


@app.get("/api/dashboard")
@login_required()
def dashboard_summary():
    db = get_db()
    role = g.current_user["role"]
    if role == "Admin":
        complaints = db.execute(
            """SELECT COUNT(*) AS total,
            SUM(CASE WHEN status = 'Submitted' THEN 1 ELSE 0 END) AS submitted,
            SUM(CASE WHEN status = 'In Progress' THEN 1 ELSE 0 END) AS in_progress,
            SUM(CASE WHEN status = 'Resolved' THEN 1 ELSE 0 END) AS resolved FROM complaints"""
        ).fetchone()
        user_counts = db.execute(
            """SELECT COUNT(*) AS total,
            SUM(CASE WHEN role = 'Student' THEN 1 ELSE 0 END) AS students,
            SUM(CASE WHEN role = 'Faculty' THEN 1 ELSE 0 END) AS faculty
            FROM users WHERE active = 1"""
        ).fetchone()
        return jsonify({"complaints": dict(complaints), "users": dict(user_counts)})
    if role == "Faculty":
        complaints = db.execute(
            """SELECT COUNT(*) AS total,
            SUM(CASE WHEN status = 'Submitted' THEN 1 ELSE 0 END) AS submitted,
            SUM(CASE WHEN status = 'In Progress' THEN 1 ELSE 0 END) AS in_progress,
            SUM(CASE WHEN status = 'Resolved' THEN 1 ELSE 0 END) AS resolved FROM complaints"""
        ).fetchone()
        return jsonify({"complaints": dict(complaints)})
    if role == "Student":
        complaints = db.execute(
            """SELECT COUNT(*) AS total,
            SUM(CASE WHEN status = 'Submitted' THEN 1 ELSE 0 END) AS submitted,
            SUM(CASE WHEN status = 'In Progress' THEN 1 ELSE 0 END) AS in_progress,
            SUM(CASE WHEN status = 'Resolved' THEN 1 ELSE 0 END) AS resolved
            FROM complaints WHERE student_id = ?""",
            (g.current_user["id"],),
        ).fetchone()
        attendance = db.execute(
            "SELECT COUNT(*) AS total FROM attendance_records WHERE student_id = ?",
            (g.current_user["id"],),
        ).fetchone()["total"]
        return jsonify({"complaints": dict(complaints), "attendance": attendance})
    placed = db.execute("SELECT COUNT(*) AS count FROM placement_records").fetchone()["count"]
    drives = db.execute("SELECT COUNT(*) AS count FROM placement_drives WHERE status = 'Open'").fetchone()["count"]
    companies = db.execute("SELECT COUNT(*) AS count FROM companies").fetchone()["count"]
    return jsonify({"placedStudents": placed, "openDrives": drives, "companies": companies})


@app.cli.command("init-db")
def init_db_command():
    initialize_database()
    print(f"Database initialized at {DATABASE}")


@app.cli.command("create-user")
@click.option("--email", required=True)
@click.option("--name", required=True)
@click.option("--role", type=click.Choice(sorted(ROLES)), required=True)
@click.option("--student-id")
@click.option("--employee-id")
def create_user_command(email, name, role, student_id, employee_id):
    from getpass import getpass

    password = getpass("Password (minimum 10 characters): ")
    if len(password) < 10:
        raise SystemExit("Password must be at least 10 characters.")
    initialize_database()
    with app.app_context():
        get_db().execute(
            """INSERT INTO users
            (name, email, password_hash, role, student_id, employee_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                name.strip(), email.strip().lower(), generate_password_hash(password),
                role, student_id, employee_id, utc_now(),
            ),
        )
    print(f"Created {role} account for {email}")


register_smart_features(app, {
    "get_db": get_db, "login_required": login_required, "api_error": api_error,
    "payload": payload, "utc_now": utc_now, "add_notification": add_notification,
})

if __name__ == "__main__":
    initialize_database()
    app.run(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG", "").lower() == "true",
    )
