import io
import ipaddress
import json
import hashlib
import math
import os
import secrets
from datetime import datetime, timedelta, timezone

from flask import g, jsonify, request, send_file


SMART_SCHEMA = """
CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    faculty_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    class_name TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    due_at TEXT NOT NULL,
    max_marks REAL NOT NULL DEFAULT 100,
    attachment_name TEXT NOT NULL DEFAULT '',
    attachment_type TEXT NOT NULL DEFAULT '',
    attachment BLOB,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assignment_submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    submitted_at TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    file_name TEXT NOT NULL DEFAULT '',
    file_type TEXT NOT NULL DEFAULT '',
    file_data BLOB,
    marks REAL,
    feedback TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'Submitted',
    UNIQUE(assignment_id, student_id)
);
CREATE TABLE IF NOT EXISTS timetable (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    class_name TEXT NOT NULL,
    day_of_week TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    subject TEXT NOT NULL,
    room TEXT NOT NULL DEFAULT '',
    faculty_id INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    class_name TEXT NOT NULL,
    subject TEXT NOT NULL,
    exam_name TEXT NOT NULL,
    exam_date TEXT NOT NULL,
    start_time TEXT NOT NULL DEFAULT '',
    end_time TEXT NOT NULL DEFAULT '',
    room TEXT NOT NULL DEFAULT '',
    total_marks REAL NOT NULL DEFAULT 100,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resumes (
    student_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    headline TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    skills TEXT NOT NULL DEFAULT '',
    education TEXT NOT NULL DEFAULT '',
    projects TEXT NOT NULL DEFAULT '',
    certifications TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eligibility_rules (
    drive_id INTEGER PRIMARY KEY REFERENCES placement_drives(id) ON DELETE CASCADE,
    min_cgpa REAL,
    min_attendance REAL,
    allowed_departments TEXT NOT NULL DEFAULT '',
    graduation_year TEXT NOT NULL DEFAULT '',
    backlog_limit INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS complaint_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    complaint_id TEXT NOT NULL REFERENCES complaints(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    updated_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS complaint_feedback (
    complaint_id TEXT PRIMARY KEY REFERENCES complaints(id) ON DELETE CASCADE,
    rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
    comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id),
    action TEXT NOT NULL,
    entity TEXT NOT NULL DEFAULT '',
    entity_id TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '',
    ip_address TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attendance_devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES attendance_sessions(id) ON DELETE CASCADE,
    student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_hash TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    UNIQUE(session_id, student_id)
);
CREATE TABLE IF NOT EXISTS face_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES attendance_sessions(id) ON DELETE CASCADE,
    student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    face_detected INTEGER NOT NULL DEFAULT 0,
    image_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(session_id, student_id)
);
CREATE INDEX IF NOT EXISTS idx_assignments_class_due ON assignments(class_name, due_at);
CREATE INDEX IF NOT EXISTS idx_timetable_class_day ON timetable(class_name, day_of_week);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at);
"""


def register_smart_features(app, helpers):
    get_db = helpers["get_db"]
    login_required = helpers["login_required"]
    api_error = helpers["api_error"]
    payload = helpers["payload"]
    utc_now = helpers["utc_now"]
    add_notification = helpers["add_notification"]

    def now_dt():
        return datetime.now(timezone.utc)

    def student_class(user):
        return "{}-{}".format(user["department"] or "", user["section"] or "").strip("-")

    def audit(action, entity="", entity_id="", details=""):
        user_id = getattr(g, "current_user", None)
        get_db().execute(
            "INSERT INTO audit_logs (user_id, action, entity, entity_id, details, ip_address, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id["id"] if user_id else None, action, entity, str(entity_id), details, request.remote_addr or "", utc_now()),
        )

    def safe_json(text):
        try:
            return json.loads(text or "{}")
        except Exception:
            return {}

    def attendance_stats_for(student_id):
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE id = ?", (student_id,)).fetchone()
        cls = student_class(user)
        rows = db.execute(
            """SELECT s.subject, COUNT(*) AS total,
               COUNT(ar.id) AS present
               FROM attendance_sessions s
               LEFT JOIN attendance_records ar ON ar.session_id=s.id AND ar.student_id=?
               WHERE lower(s.class_name)=lower(?)
               GROUP BY s.subject ORDER BY s.subject""",
            (student_id, cls),
        ).fetchall()
        total = sum(r["total"] for r in rows)
        present = sum(r["present"] for r in rows)
        percentage = round(present / total * 100, 2) if total else 0
        required = 75
        needed = max(0, math.ceil((required * total / 100 - present) / (1 - required / 100))) if total and percentage < required else 0
        missed = max(0, total - present)
        subjects = []
        for r in rows:
            p = round(r["present"] / r["total"] * 100, 2) if r["total"] else 0
            n = max(0, math.ceil((required * r["total"] / 100 - r["present"]) / (1 - required / 100))) if p < required and r["total"] else 0
            subjects.append({"subject": r["subject"], "total": r["total"], "present": r["present"], "missed": r["total"]-r["present"], "percentage": p, "classesNeeded": n})
        return {"overall": percentage, "totalClasses": total, "present": present, "missed": missed, "required": required, "classesNeeded": needed, "subjects": subjects}

    def maybe_low_attendance_notification(student_id):
        stats = attendance_stats_for(student_id)
        if stats["overall"] < stats["required"] and stats["totalClasses"]:
            db = get_db()
            today = datetime.now(timezone.utc).date().isoformat()
            exists = db.execute(
                "SELECT 1 FROM notifications WHERE user_id=? AND type='attendance-warning' AND created_at LIKE ? LIMIT 1",
                (student_id, today + "%"),
            ).fetchone()
            if not exists:
                add_notification(student_id, "Low attendance alert", f"Your attendance is {stats['overall']}%. You need {stats['classesNeeded']} more classes to reach 75%.", "attendance-warning")

    def allowed_network():
        raw = os.environ.get("CAMPUS_ALLOWED_NETWORKS", "").strip()
        if not raw:
            return True
        ip = request.remote_addr
        if not ip:
            return False
        try:
            addr = ipaddress.ip_address(ip)
            return any(addr in ipaddress.ip_network(x.strip(), strict=False) for x in raw.split(",") if x.strip())
        except ValueError:
            return False

    @app.get("/api/smart/attendance/dashboard")
    @login_required("Student")
    def smart_attendance_dashboard():
        stats = attendance_stats_for(g.current_user["id"])
        maybe_low_attendance_notification(g.current_user["id"])
        return jsonify(stats)

    @app.patch("/api/attendance/sessions/<token>")
    @login_required("Faculty", "Admin")
    def smart_attendance_session_control(token):
        data = payload()
        action = str(data.get("action", "")).lower()
        db = get_db()
        row = db.execute("SELECT * FROM attendance_sessions WHERE token=?", (token,)).fetchone()
        if not row:
            return api_error("Attendance session not found.", 404)
        if g.current_user["role"] == "Faculty" and row["faculty_id"] != g.current_user["id"]:
            return api_error("You can only control your own attendance sessions.", 403)
        if action == "close":
            db.execute("UPDATE attendance_sessions SET status='Closed', closed_at=? WHERE id=?", (utc_now(), row["id"]))
        elif action == "reopen":
            expires = now_dt() + timedelta(minutes=5)
            db.execute("UPDATE attendance_sessions SET status='Open', closed_at=NULL, expires_at=? WHERE id=?", (expires.isoformat(), row["id"]))
        else:
            return api_error("Action must be close or reopen.")
        audit("attendance_session_" + action, "attendance_session", row["id"], row["class_name"] + " / " + row["subject"])
        return jsonify({"ok": True, "status": "Closed" if action == "close" else "Open"})

    @app.get("/api/assignments")
    @login_required()
    def list_assignments():
        db = get_db()
        if g.current_user["role"] == "Student":
            cls = student_class(g.current_user)
            rows = db.execute("""SELECT a.*, s.id AS submission_id, s.submitted_at, s.marks, s.feedback, s.status AS submission_status
                FROM assignments a LEFT JOIN assignment_submissions s ON s.assignment_id=a.id AND s.student_id=?
                WHERE lower(a.class_name)=lower(?) OR a.class_name='' ORDER BY a.due_at""", (g.current_user["id"], cls)).fetchall()
        else:
            rows = db.execute("SELECT * FROM assignments ORDER BY due_at").fetchall()
        out=[]
        for r in rows:
            out.append({"id":r["id"],"title":r["title"],"description":r["description"],"class":r["class_name"],"subject":r["subject"],"dueAt":r["due_at"],"maxMarks":r["max_marks"],"hasAttachment":bool(r["attachment"]),"attachmentName":r["attachment_name"],"submissionId":r["submission_id"] if "submission_id" in r.keys() else None,"submittedAt":r["submitted_at"] if "submitted_at" in r.keys() else None,"marks":r["marks"] if "marks" in r.keys() else None,"feedback":r["feedback"] if "feedback" in r.keys() else "","submissionStatus":r["submission_status"] if "submission_status" in r.keys() else None})
        return jsonify({"assignments":out})

    @app.post("/api/assignments")
    @login_required("Faculty", "Admin")
    def create_assignment():
        form = request.form
        title = form.get("title", "").strip(); due = form.get("dueAt", "").strip()
        if not title or not due: return api_error("Title and deadline are required.")
        f = request.files.get("file")
        content = f.read() if f else None
        cur=get_db().execute("INSERT INTO assignments (faculty_id,title,description,class_name,subject,due_at,max_marks,attachment_name,attachment_type,attachment,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (g.current_user["id"],title,form.get("description","").strip(),form.get("className","").strip(),form.get("subject","").strip(),due,float(form.get("maxMarks") or 100),f.filename if f else "",f.content_type if f else "",content,utc_now()))
        students=get_db().execute("SELECT id FROM users WHERE role='Student' AND active=1 AND (?='' OR lower(COALESCE(department,'')||'-'||COALESCE(section,''))=lower(?))", (form.get("className","").strip(),form.get("className","").strip())).fetchall()
        for st in students: add_notification(st["id"], "New assignment", f'{title} is due {due}.', "assignment")
        audit("assignment_created","assignment",cur.lastrowid,title)
        return jsonify({"id":cur.lastrowid}),201

    @app.get("/api/assignments/<int:assignment_id>/file")
    @login_required()
    def assignment_file(assignment_id):
        row=get_db().execute("SELECT attachment,attachment_type,attachment_name FROM assignments WHERE id=?",(assignment_id,)).fetchone()
        if not row or row["attachment"] is None: return api_error("Attachment not found.",404)
        return send_file(io.BytesIO(row["attachment"]),mimetype=row["attachment_type"] or "application/octet-stream",download_name=row["attachment_name"] or "assignment-file")

    @app.post("/api/assignments/<int:assignment_id>/submit")
    @login_required("Student")
    def submit_assignment(assignment_id):
        row=get_db().execute("SELECT * FROM assignments WHERE id=?",(assignment_id,)).fetchone()
        if not row: return api_error("Assignment not found.",404)
        f=request.files.get("file"); content=f.read() if f else None
        now=utc_now()
        get_db().execute("INSERT INTO assignment_submissions (assignment_id,student_id,submitted_at,content,file_name,file_type,file_data) VALUES (?,?,?,?,?,?,?) ON CONFLICT(assignment_id,student_id) DO UPDATE SET submitted_at=excluded.submitted_at,content=excluded.content,file_name=excluded.file_name,file_type=excluded.file_type,file_data=excluded.file_data,status='Resubmitted'",(assignment_id,g.current_user["id"],now,request.form.get("content","").strip(),f.filename if f else "",f.content_type if f else "",content))
        add_notification(row["faculty_id"], "Assignment submission received", f'{g.current_user["name"]} submitted {row["title"]}.', "assignment")
        audit("assignment_submitted","assignment",assignment_id,row["title"])
        return jsonify({"ok":True,"submittedAt":now}),201

    @app.patch("/api/assignments/submissions/<int:submission_id>")
    @login_required("Faculty", "Admin")
    def grade_assignment(submission_id):
        data=payload()
        try: marks=float(data.get("marks"))
        except (TypeError,ValueError): return api_error("Marks must be numeric.")
        sub=get_db().execute("SELECT s.*,a.title,a.max_marks,s.student_id FROM assignment_submissions s JOIN assignments a ON a.id=s.assignment_id WHERE s.id=?",(submission_id,)).fetchone()
        if not sub: return api_error("Submission not found.",404)
        if marks<0 or marks>sub["max_marks"]: return api_error("Marks are outside the allowed range.")
        get_db().execute("UPDATE assignment_submissions SET marks=?,feedback=?,status='Evaluated' WHERE id=?",(marks,str(data.get("feedback","")).strip(),submission_id))
        add_notification(sub["student_id"],"Assignment evaluated",f'{sub["title"]} has been evaluated: {marks}/{sub["max_marks"]}.',"assignment")
        audit("assignment_graded","submission",submission_id,str(marks))
        return jsonify({"ok":True})

    @app.get("/api/timetable")
    @login_required()
    def list_timetable():
        cls=student_class(g.current_user) if g.current_user["role"]=="Student" else str(request.args.get("class","")).strip()
        rows=get_db().execute("SELECT t.*,u.name AS faculty_name FROM timetable t LEFT JOIN users u ON u.id=t.faculty_id WHERE lower(t.class_name)=lower(?) OR t.class_name='' ORDER BY CASE t.day_of_week WHEN 'Monday' THEN 1 WHEN 'Tuesday' THEN 2 WHEN 'Wednesday' THEN 3 WHEN 'Thursday' THEN 4 WHEN 'Friday' THEN 5 WHEN 'Saturday' THEN 6 ELSE 7 END,t.start_time",(cls,)).fetchall()
        return jsonify({"timetable":[{"id":r["id"],"day":r["day_of_week"],"startTime":r["start_time"],"endTime":r["end_time"],"subject":r["subject"],"room":r["room"],"faculty":r["faculty_name"] or ""} for r in rows]})

    @app.post("/api/timetable")
    @login_required("Faculty", "Admin")
    def create_timetable():
        d=payload(); required=["className","day","startTime","endTime","subject"]
        if any(not str(d.get(x,"")).strip() for x in required): return api_error("Class, day, time and subject are required.")
        cur=get_db().execute("INSERT INTO timetable (class_name,day_of_week,start_time,end_time,subject,room,faculty_id,created_at) VALUES (?,?,?,?,?,?,?,?)",(str(d["className"]).strip(),str(d["day"]).strip(),str(d["startTime"]).strip(),str(d["endTime"]).strip(),str(d["subject"]).strip(),str(d.get("room","")).strip(),g.current_user["id"],utc_now()))
        return jsonify({"id":cur.lastrowid}),201

    @app.delete("/api/timetable/<int:item_id>")
    @login_required("Faculty", "Admin")
    def delete_timetable(item_id):
        get_db().execute("DELETE FROM timetable WHERE id=?",(item_id,)); return jsonify({"ok":True})

    @app.get("/api/exams")
    @login_required()
    def list_exams():
        cls=student_class(g.current_user) if g.current_user["role"]=="Student" else str(request.args.get("class","")).strip()
        rows=get_db().execute("SELECT * FROM exams WHERE lower(class_name)=lower(?) OR class_name='' ORDER BY exam_date,start_time",(cls,)).fetchall()
        return jsonify({"exams":[{"id":r["id"],"class":r["class_name"],"subject":r["subject"],"examName":r["exam_name"],"date":r["exam_date"],"startTime":r["start_time"],"endTime":r["end_time"],"room":r["room"],"totalMarks":r["total_marks"]} for r in rows]})

    @app.post("/api/exams")
    @login_required("Faculty", "Admin")
    def create_exam():
        d=payload(); cur=get_db().execute("INSERT INTO exams (class_name,subject,exam_name,exam_date,start_time,end_time,room,total_marks,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",(str(d.get("className","")).strip(),str(d.get("subject","")).strip(),str(d.get("examName","")).strip(),str(d.get("date","")).strip(),str(d.get("startTime","")).strip(),str(d.get("endTime","")).strip(),str(d.get("room","")).strip(),float(d.get("totalMarks") or 100),g.current_user["id"],utc_now()))
        return jsonify({"id":cur.lastrowid}),201

    @app.get("/api/results/academic-summary")
    @login_required("Student")
    def academic_summary():
        db=get_db(); rows=db.execute("SELECT * FROM results WHERE student_id=? ORDER BY created_at",(g.current_user["id"],)).fetchall()
        by_subject={}
        for r in rows:
            b=by_subject.setdefault(r["subject"],{"obtained":0,"total":0})
            b["obtained"]+=r["obtained_marks"]; b["total"]+=r["total_marks"]
        entries=[]
        for sub,b in by_subject.items():
            pct=b["obtained"]/b["total"]*100 if b["total"] else 0
            grade='A+' if pct>=90 else 'A' if pct>=80 else 'B' if pct>=70 else 'C' if pct>=60 else 'D' if pct>=50 else 'F'
            entries.append({"subject":sub,"obtained":round(b["obtained"],2),"total":round(b["total"],2),"percentage":round(pct,2),"grade":grade})
        avg=sum(x["percentage"] for x in entries)/len(entries) if entries else 0
        gpa=round(avg/10,2)
        return jsonify({"subjects":entries,"averagePercentage":round(avg,2),"gpa":gpa,"cgpa":gpa})

    @app.get("/api/placements/eligibility")
    @login_required("Student", "Placement Officer", "Admin")
    def placement_eligibility():
        db=get_db(); student_id=g.current_user["id"]
        if g.current_user["role"]=="Student":
            stats=attendance_stats_for(student_id); summary=db.execute("SELECT COALESCE(AVG(obtained_marks*100.0/total_marks),0) AS p FROM results WHERE student_id=?",(student_id,)).fetchone(); cgpa=float(summary["p"] or 0)/10
            rows=db.execute("SELECT d.*,c.name company,e.min_cgpa,e.min_attendance,e.allowed_departments,e.graduation_year,e.backlog_limit FROM placement_drives d JOIN companies c ON c.id=d.company_id LEFT JOIN eligibility_rules e ON e.drive_id=d.id WHERE d.status='Open' ORDER BY d.drive_date").fetchall()
            out=[]
            for r in rows:
                allowed=[x.strip().casefold() for x in (r["allowed_departments"] or '').split(',') if x.strip()]
                ok=(r["min_cgpa"] is None or cgpa>=r["min_cgpa"]) and (r["min_attendance"] is None or stats["overall"]>=r["min_attendance"]) and (not allowed or (g.current_user["department"] or '').casefold() in allowed)
                out.append({"driveId":r["id"],"title":r["title"],"company":r["company"],"eligible":ok,"reasons":[] if ok else ["Does not meet one or more eligibility rules"],"cgpa":round(cgpa,2),"attendance":stats["overall"]})
            return jsonify({"eligibility":out})
        rows=db.execute("SELECT d.id,d.title,c.name,e.min_cgpa,e.min_attendance,e.allowed_departments FROM placement_drives d JOIN companies c ON c.id=d.company_id LEFT JOIN eligibility_rules e ON e.drive_id=d.id ORDER BY d.created_at DESC").fetchall()
        return jsonify({"eligibility":[dict(r) for r in rows]})

    @app.put("/api/placements/drives/<int:drive_id>/eligibility")
    @login_required("Placement Officer", "Admin")
    def set_eligibility(drive_id):
        d=payload(); get_db().execute("INSERT INTO eligibility_rules (drive_id,min_cgpa,min_attendance,allowed_departments,graduation_year,backlog_limit) VALUES (?,?,?,?,?,?) ON CONFLICT(drive_id) DO UPDATE SET min_cgpa=excluded.min_cgpa,min_attendance=excluded.min_attendance,allowed_departments=excluded.allowed_departments,graduation_year=excluded.graduation_year,backlog_limit=excluded.backlog_limit",(drive_id,float(d["minCgpa"]) if d.get("minCgpa") not in (None,'') else None,float(d["minAttendance"]) if d.get("minAttendance") not in (None,'') else None,str(d.get("departments","")).strip(),str(d.get("graduationYear","")).strip(),int(d.get("backlogLimit") or 0)))
        return jsonify({"ok":True})

    @app.get("/api/resume")
    @login_required("Student")
    def get_resume():
        row=get_db().execute("SELECT * FROM resumes WHERE student_id=?",(g.current_user["id"],)).fetchone()
        return jsonify({"resume":dict(row) if row else {"student_id":g.current_user["id"]}})

    @app.put("/api/resume")
    @login_required("Student")
    def save_resume():
        d=payload(); get_db().execute("INSERT INTO resumes (student_id,headline,summary,skills,education,projects,certifications,updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(student_id) DO UPDATE SET headline=excluded.headline,summary=excluded.summary,skills=excluded.skills,education=excluded.education,projects=excluded.projects,certifications=excluded.certifications,updated_at=excluded.updated_at",(g.current_user["id"],str(d.get("headline","")).strip(),str(d.get("summary","")).strip(),str(d.get("skills","")).strip(),str(d.get("education","")).strip(),str(d.get("projects","")).strip(),str(d.get("certifications","")).strip(),utc_now()))
        return jsonify({"ok":True})

    @app.get("/api/resume/pdf")
    @login_required("Student")
    def resume_pdf():
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.pdfgen import canvas
        except ImportError:
            return api_error("PDF support requires reportlab. Run pip install -r requirements.txt.",500)
        r=get_db().execute("SELECT * FROM resumes WHERE student_id=?",(g.current_user["id"],)).fetchone()
        if not r: return api_error("Save your resume first.",404)
        buf=io.BytesIO(); c=canvas.Canvas(buf,pagesize=A4); w,h=A4; y=h-50
        c.setFont("Helvetica-Bold",18); c.drawString(45,y,g.current_user["name"]); y-=24
        c.setFont("Helvetica",10); c.drawString(45,y,g.current_user["email"]); y-=30
        for title,key in [("Headline","headline"),("Summary","summary"),("Skills","skills"),("Education","education"),("Projects","projects"),("Certifications","certifications")]:
            c.setFont("Helvetica-Bold",12); c.drawString(45,y,title); y-=16; c.setFont("Helvetica",10)
            for line in str(r[key] or '').splitlines() or ['']:
                c.drawString(55,y,line[:110]); y-=14
                if y<55: c.showPage(); y=h-50
            y-=8
        c.save(); buf.seek(0); return send_file(buf,as_attachment=True,download_name="student-resume.pdf",mimetype="application/pdf")

    @app.get("/api/complaints/<complaint_id>/timeline")
    @login_required()
    def complaint_timeline(complaint_id):
        db=get_db(); c=db.execute("SELECT student_id FROM complaints WHERE id=?",(complaint_id,)).fetchone()
        if not c: return api_error("Complaint not found.",404)
        if g.current_user["role"]=="Student" and c["student_id"]!=g.current_user["id"]: return api_error("Not authorized.",403)
        rows=db.execute("SELECT cu.*,u.name updater FROM complaint_updates cu LEFT JOIN users u ON u.id=cu.updated_by WHERE complaint_id=? ORDER BY created_at",(complaint_id,)).fetchall()
        return jsonify({"timeline":[{"status":r["status"],"note":r["note"],"updatedBy":r["updater"] or "System","createdAt":r["created_at"]} for r in rows]})

    @app.post("/api/complaints/<complaint_id>/feedback")
    @login_required("Student")
    def complaint_feedback(complaint_id):
        d=payload(); rating=int(d.get("rating") or 0)
        c=get_db().execute("SELECT student_id FROM complaints WHERE id=?",(complaint_id,)).fetchone()
        if not c or c["student_id"]!=g.current_user["id"]: return api_error("Complaint not found.",404)
        if rating<1 or rating>5: return api_error("Rating must be between 1 and 5.")
        get_db().execute("INSERT INTO complaint_feedback (complaint_id,rating,comment,created_at) VALUES (?,?,?,?) ON CONFLICT(complaint_id) DO UPDATE SET rating=excluded.rating,comment=excluded.comment,created_at=excluded.created_at",(complaint_id,rating,str(d.get("comment","")).strip(),utc_now()))
        return jsonify({"ok":True})

    @app.post("/api/ai/assistant")
    @login_required("Student", "Faculty", "Placement Officer", "Admin")
    def ai_assistant():
        """Role-aware campus assistant.

        The assistant intentionally queries only data appropriate to the
        signed-in role. It never accepts a user-supplied user_id/student_id
        to switch identity.
        """
        raw_q = str(payload().get("message", "")).strip()
        q = raw_q.casefold()
        db = get_db()
        role = g.current_user["role"]
        if not q:
            return api_error("Ask a question first.")

        # Hard role boundary: the assistant is not a general file/database
        # reader. Each role gets an explicit topic allow-list before any
        # role-specific query is executed. This prevents questions about
        # another portal area from falling through to unrelated data.
        topic_words = {
            "Student": {
                "attendance": ("attendance", "present", "absent", "missed", "class"),
                "assignments": ("assignment", "homework", "submission", "due"),
                "exams": ("exam", "test", "timetable", "schedule"),
                "results": ("result", "marks", "grade", "cgpa", "percentage"),
                "placements": ("placement", "company", "drive", "eligible", "eligibility"),
            },
            "Faculty": {
                "attendance": ("attendance", "present", "absent"),
                "assignments": ("assignment", "homework", "submission", "due"),
                "exams": ("exam", "test", "timetable", "schedule"),
                "results": ("result", "marks", "grade", "student marks"),
            },
            "Placement Officer": {
                "placements": ("placement", "company", "drive", "eligible", "eligibility", "placed", "package"),
            },
            "Admin": {
                "users": ("user", "users", "account", "accounts", "student count", "faculty count", "placement officer"),
                "attendance": ("attendance",),
                "complaints": ("complaint", "complaints"),
                "placements": ("placement", "placements", "company", "companies", "drive", "drives"),
                "system": ("system", "database", "health", "security"),
            },
        }
        scopes = topic_words.get(role, {})
        requested_topic = next((topic for topic, words in scopes.items() if any(w in q for w in words)), None)
        if requested_topic is None:
            allowed = {
                "Student": "your attendance, assignments, exams, results/CGPA and placement drives",
                "Faculty": "your teaching attendance, assignments, exam schedules and results",
                "Placement Officer": "placement drives, companies, eligibility and placement outcomes",
                "Admin": "user/account summaries, attendance, complaints, placements and system summaries",
            }.get(role, "your permitted portal data")
            return jsonify({"answer": f"I can only answer questions within your {role} portal scope. I can help with {allowed}.", "source": "Role-scoped Smart Campus portal data", "role": role})

        # STUDENT: only the signed-in student's own academic/placement data.
        if role == "Student":
            stats = attendance_stats_for(g.current_user["id"])
            if requested_topic == "attendance":
                answer = f"Your attendance is {stats['overall']}%. You have attended {stats['present']} of {stats['totalClasses']} classes."
            elif "miss" in q and "class" in q:
                answer = f"At a 75% requirement, you can currently miss about {max(0, math.floor(stats['present'] / 0.75 - stats['totalClasses']))} additional classes before dropping below the requirement."
            elif requested_topic == "assignments":
                rows = db.execute(
                    """SELECT title, due_at FROM assignments a
                       WHERE (lower(a.class_name)=lower(?) OR a.class_name='')
                       AND NOT EXISTS (
                           SELECT 1 FROM assignment_submissions s
                           WHERE s.assignment_id=a.id AND s.student_id=?
                       ) ORDER BY due_at""",
                    (student_class(g.current_user), g.current_user["id"]),
                ).fetchall()
                answer = "Pending assignments: " + ("; ".join(f"{r['title']} (due {r['due_at']})" for r in rows) if rows else "None.")
            elif requested_topic == "exams":
                rows = db.execute(
                    """SELECT exam_name, subject, exam_date, start_time FROM exams
                       WHERE lower(class_name)=lower(?) AND exam_date>=date('now')
                       ORDER BY exam_date,start_time LIMIT 5""",
                    (student_class(g.current_user),),
                ).fetchall()
                answer = "Upcoming exams: " + ("; ".join(f"{r['exam_name']} - {r['subject']} on {r['exam_date']} {r['start_time']}" for r in rows) if rows else "No upcoming exams found.")
            elif requested_topic == "results":
                avg = db.execute(
                    "SELECT COALESCE(SUM(obtained_marks)*100.0/NULLIF(SUM(total_marks),0),0) p FROM results WHERE student_id=?",
                    (g.current_user["id"],),
                ).fetchone()["p"]
                answer = f"Your recorded average is {float(avg)/10:.2f} CGPA (derived from published result marks)."
            elif requested_topic == "placements":
                rows = db.execute(
                    """SELECT d.title,c.name FROM placement_drives d
                       JOIN companies c ON c.id=d.company_id
                       WHERE d.status='Open' ORDER BY d.drive_date"""
                ).fetchall()
                answer = "Open placement drives: " + ("; ".join(f"{r['name']} - {r['title']}" for r in rows) if rows else "None currently open.")
            else:
                answer = "I can help with your attendance, assignments, exams, results/CGPA and placement drives."

        # FACULTY: only their assigned teaching data plus aggregate class data.
        elif role == "Faculty":
            faculty_id = g.current_user["id"]
            assigned_class = str(g.current_user["assigned_class"] or "").strip()
            assigned_subject = str(g.current_user["subject"] or "").strip()
            if requested_topic == "attendance":
                row = db.execute(
                    """SELECT COUNT(DISTINCT ar.student_id) present, COUNT(DISTINCT u.id) students
                       FROM users u LEFT JOIN attendance_records ar ON ar.student_id=u.id
                       LEFT JOIN attendance_sessions s ON s.id=ar.session_id
                       WHERE u.role='Student' AND u.active=1
                       AND (?='' OR lower(u.department||'-'||u.section)=lower(?))
                       AND (s.faculty_id=? OR s.faculty_id IS NULL)""",
                    (assigned_class, assigned_class, faculty_id),
                ).fetchone()
                answer = f"Your assigned class is {assigned_class or 'not specified'} with {row['students'] or 0} active students and {row['present'] or 0} students with recorded attendance in your sessions."
            elif requested_topic == "assignments":
                rows = db.execute(
                    """SELECT title,due_at,class_name FROM assignments
                       WHERE faculty_id=? OR (?<>'' AND lower(class_name)=lower(?))
                       ORDER BY due_at LIMIT 10""",
                    (faculty_id, assigned_class, assigned_class),
                ).fetchall()
                answer = "Your assignments: " + ("; ".join(f"{r['title']} ({r['class_name'] or 'all classes'}, due {r['due_at']})" for r in rows) if rows else "No assignments found for your teaching scope.")
            elif requested_topic == "exams":
                rows = db.execute(
                    """SELECT exam_name,subject,exam_date,start_time,class_name FROM exams
                       WHERE (created_by=? OR (?<>'' AND lower(class_name)=lower(?)))
                       ORDER BY exam_date,start_time LIMIT 10""",
                    (faculty_id, assigned_class, assigned_class),
                ).fetchall()
                answer = "Relevant exam schedule: " + ("; ".join(f"{r['exam_name']} - {r['subject']} - {r['exam_date']} {r['start_time']} ({r['class_name']})" for r in rows) if rows else "No matching exam schedule found.")
            elif requested_topic == "results":
                rows = db.execute(
                    """SELECT COUNT(*) total, COALESCE(AVG(obtained_marks*100.0/NULLIF(total_marks,0)),0) avg_pct
                       FROM results WHERE entered_by=?""",
                    (faculty_id,),
                ).fetchone()
                answer = f"You have entered {rows['total'] or 0} result records. Their average recorded percentage is {float(rows['avg_pct'] or 0):.1f}%."
            else:
                answer = f"I can help with your teaching scope: {assigned_subject or 'subject'}, {assigned_class or 'assigned class'}, attendance, assignments, exam schedules and results."

        # PLACEMENT OFFICER: placement management data only.
        elif role == "Placement Officer":
            if requested_topic == "placements":
                rows = db.execute(
                    """SELECT c.name,d.title,d.status,d.drive_date FROM placement_drives d
                       JOIN companies c ON c.id=d.company_id ORDER BY d.drive_date LIMIT 15"""
                ).fetchall()
                answer = "Placement drives: " + ("; ".join(f"{r['name']} - {r['title']} ({r['status']}, {r['drive_date']})" for r in rows) if rows else "No placement drives found.")
            elif requested_topic == "placements":
                row = db.execute("SELECT COUNT(*) c FROM placement_records").fetchone()
                answer = f"There are {row['c'] or 0} recorded placement outcomes. Use the Placement module for eligibility and drive details."
            else:
                answer = "I can help with placement drives, companies, eligibility and placement outcomes."

        # ADMIN: campus-wide operational data, but still through explicit
        # read-only summaries rather than exposing database/file contents.
        else:
            if requested_topic == "users":
                row = db.execute(
                    """SELECT COUNT(*) total,
                       SUM(role='Student') students,
                       SUM(role='Faculty') faculty,
                       SUM(role='Placement Officer') placement,
                       SUM(role='Admin') admins
                       FROM users WHERE active=1"""
                ).fetchone()
                answer = f"Active accounts: {row['total'] or 0} total — {row['students'] or 0} students, {row['faculty'] or 0} faculty, {row['placement'] or 0} placement officers and {row['admins'] or 0} admins."
            elif requested_topic == "attendance":
                row = db.execute("SELECT COUNT(*) sessions FROM attendance_sessions").fetchone()
                records = db.execute("SELECT COUNT(*) records FROM attendance_records").fetchone()
                answer = f"The portal has {row['sessions'] or 0} attendance sessions and {records['records'] or 0} attendance records."
            elif requested_topic == "complaints":
                row = db.execute("SELECT COUNT(*) total, SUM(status='Submitted') submitted, SUM(status='In Progress') progress, SUM(status='Resolved') resolved FROM complaints").fetchone()
                answer = f"Complaints: {row['total'] or 0} total, {row['submitted'] or 0} submitted, {row['progress'] or 0} in progress and {row['resolved'] or 0} resolved."
            elif requested_topic == "placements":
                row = db.execute("SELECT COUNT(*) c FROM placement_drives WHERE status='Open'").fetchone()
                answer = f"There are {row['c'] or 0} open placement drives. Use the Placement Officer module for detailed drive management."
            elif requested_topic == "system":
                tables = db.execute("SELECT COUNT(*) c FROM sqlite_master WHERE type='table'").fetchone()["c"]
                answer = f"The campus database currently contains {tables} tables. System health details are available in the Security Audit module."
            else:
                answer = "I can help with campus users, attendance, complaints, placements and system summaries. I do not expose raw files or unrestricted database contents."

        return jsonify({"answer": answer, "source": "Role-scoped Smart Campus portal data", "role": role})

    @app.post("/api/attendance/face-check")
    @login_required("Student")
    def attendance_face_check():
        token = str(request.form.get("token", "")).strip()
        photo = request.files.get("photo")
        if not token or not photo:
            return api_error("Session token and selfie are required.")
        row = get_db().execute("SELECT id, expires_at, status FROM attendance_sessions WHERE token=?", (token,)).fetchone()
        if not row or row["status"] != "Open" or row["expires_at"] <= utc_now():
            return api_error("Attendance session is closed or expired.", 410)
        data = photo.read()
        digest = hashlib.sha256(data).hexdigest()
        detected = False
        try:
            import cv2
            import numpy as np
            image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
            cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
            if image is not None and not cascade.empty():
                faces = cascade.detectMultiScale(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), 1.1, 5)
                detected = len(faces) == 1
        except Exception:
            return jsonify({"ok": False, "configured": False, "message": "Face presence verification is optional. Install opencv-python-headless to enable it."}), 200
        get_db().execute("INSERT INTO face_checks (session_id,student_id,face_detected,image_hash,created_at) VALUES (?,?,?,?,?) ON CONFLICT(session_id,student_id) DO UPDATE SET face_detected=excluded.face_detected,image_hash=excluded.image_hash,created_at=excluded.created_at", (row["id"],g.current_user["id"],int(detected),digest,utc_now()))
        return jsonify({"ok": detected, "configured": True, "faceDetected": detected, "message": "One face detected." if detected else "Please capture a clear selfie with exactly one face."})

    @app.get("/api/smart/audit")
    @login_required("Admin")
    def smart_audit():
        rows=get_db().execute("SELECT a.*,u.name FROM audit_logs a LEFT JOIN users u ON u.id=a.user_id ORDER BY a.created_at DESC LIMIT 200").fetchall(); return jsonify({"logs":[dict(r) for r in rows]})

    @app.get("/api/smart/health")
    @login_required("Admin")
    def smart_health():
        db=get_db(); return jsonify({"tables":len(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()),"students":db.execute("SELECT COUNT(*) c FROM users WHERE role='Student'").fetchone()['c'],"assignments":db.execute("SELECT COUNT(*) c FROM assignments").fetchone()['c'],"attendanceSessions":db.execute("SELECT COUNT(*) c FROM attendance_sessions").fetchone()['c'],"auditLogs":db.execute("SELECT COUNT(*) c FROM audit_logs").fetchone()['c']})
