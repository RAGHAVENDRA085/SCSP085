"""Local integration smoke tests for the Smart Campus project.
Run after: pip install -r requirements.txt
"""
import os
import tempfile
from pathlib import Path

DB = Path(tempfile.gettempdir()) / "smart-campus-smoke.db"
if DB.exists():
    DB.unlink()
os.environ["CAMPUS_DATABASE"] = str(DB)
os.environ["CAMPUS_ADMIN_KEY"] = "smoke-admin-key"
os.environ["FLASK_SECRET_KEY"] = "smoke-secret-key-change-me"

from werkzeug.security import generate_password_hash
from app import app, get_db, initialize_database, utc_now

initialize_database()
with app.app_context():
    db = get_db()
    users = [
        ("Smoke Admin", "admin@smoke.local", "Admin", None, None, None, None),
        ("Smoke Student", "student@smoke.local", "Student", "STU-SMOKE", None, "CSE", "A"),
        ("Smoke Faculty", "faculty@smoke.local", "Faculty", None, "FAC-SMOKE", "CSE", "A"),
        ("Smoke Placement", "placement@smoke.local", "Placement Officer", None, None, None, None),
    ]
    for name, email, role, sid, eid, dept, section in users:
        db.execute(
            """INSERT INTO users
            (name,email,password_hash,role,student_id,employee_id,department,section,profile_json,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (name, email, generate_password_hash("Password123!"), role, sid, eid, dept, section, "{}", utc_now()),
        )

client = app.test_client()

def login(role, email, **extra):
    payload = {"email": email, "password": "Password123!", "role": role, **extra}
    r = client.post("/api/auth/login", json=payload)
    assert r.status_code == 200, (role, r.status_code, r.get_json())
    return r.get_json()["user"]

for role, email, extra in [
    ("Student", "student@smoke.local", {}),
    ("Faculty", "faculty@smoke.local", {"employeeId": "FAC-SMOKE"}),
    ("Placement Officer", "placement@smoke.local", {}),
    ("Admin", "admin@smoke.local", {"adminKey": "smoke-admin-key"}),
]:
    user = login(role, email, **extra)
    assert user["role"] == role
    assert client.get("/api/auth/me").status_code == 200
    client.post("/api/auth/logout")

login("Student", "student@smoke.local")
r = client.post("/api/auth/login", json={"email":"faculty@smoke.local","password":"Password123!","role":"Student"})
assert r.status_code == 403
r = client.post("/api/ai/assistant", json={"message":"show faculty records"})
assert r.status_code == 200
assert "only answer questions within your Student portal scope" in r.get_json()["answer"]
client.post("/api/auth/logout")

for path in ["/app.py", "/smart_features.py", "/.env", "/instance/campus.db"]:
    r = client.get(path)
    assert r.status_code == 404, (path, r.status_code)

print("SMOKE_TEST_OK")
