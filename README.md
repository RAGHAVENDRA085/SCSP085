# Smart Campus Student Portal — Enhanced

Flask + SQLite Smart Campus portal upgraded with QR attendance, anti-proxy checks, academic modules, assignments, timetable, exams/results, placement tools, complaint tracking, AI campus assistant and security audit logs.

## Run

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open `http://localhost:5000/`. The server defaults to `0.0.0.0` so a phone on the same Wi-Fi can reach the QR scanner. Find the computer LAN IP and open `http://LAN-IP:5000/` on the phone. Windows Firewall may need an inbound rule for port 5000.

## Accounts
Use your existing accounts. To create one with Flask CLI:

```bash
flask --app app create-user --email student@example.com --name "Demo Student" --role Student --student-id STU001
```

The CLI prompts for a password of at least 10 characters.

## Added features
- Dynamic 5-minute QR attendance, close/reopen, live count and duplicate prevention.
- Anti-proxy device fingerprint, optional GPS and optional college-network CIDR restriction.
- Optional one-face presence check with OpenCV.
- Student attendance dashboard with subject percentages and 75% recovery calculation.
- Smart notifications for attendance, assignments, results and placements.
- Assignment creation, file attachment, online submission and grading.
- Timetable and class reminders.
- Exam scheduling plus result/GPA/CGPA analytics.
- Placement eligibility rules and resume builder with PDF export.
- Complaint timeline and student feedback.
- Portal-data AI Campus Assistant.
- Admin audit logs and health dashboard.

## Anti-proxy
Set `CAMPUS_ALLOWED_NETWORKS` to comma-separated CIDR blocks if attendance should be restricted to a college network. Leave blank to disable the restriction. GPS is permission-based and only recorded when the student grants access.

Face verification here means one-face presence detection, not biometric identity matching. Real biometric matching should use a properly consented identity provider and applicable privacy controls.

## Real-time
The faculty QR screen polls every 3 seconds for live attendance counts, keeping deployment simple without adding a WebSocket service.

## Future scope
Phase 1: Smart Campus — QR attendance, timetable, assignments, exams, results, notifications and complaints.
Phase 2: Intelligent Campus — AI chatbot, prediction, personalized alerts and academic recommendations.
Phase 3: Mobile app — Android/iOS push notifications, QR scanner and offline-friendly services.
Phase 4: IoT Smart Campus — RFID/NFC, smart classrooms, occupancy and energy sensors, smart library.
Phase 5: Advanced security — MFA, biometric identity verification, stronger device risk scoring, tamper-resistant audit storage and anomaly detection.
