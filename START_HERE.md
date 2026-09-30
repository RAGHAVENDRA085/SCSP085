# Smart Campus Student Portal — Final Build

## What is fixed in this build

- All four login portals use the Flask server authentication system:
  - Student
  - Faculty
  - Placement Officer
  - Admin
- Faculty login requires the server-side Employee ID check.
- Admin login requires the server-side administrator key.
- Student login no longer accepts fake/localStorage-only credentials.
- Student, Faculty and Admin dashboards verify the active Flask session before loading.
- Placement Officer dashboard already uses server-side session verification.
- AI Campus Assistant is role-scoped and does not read arbitrary project files.
- Direct requests for Python, database, environment/config and other non-web project files are blocked.
- Security response headers are enabled.
- SQLite foreign keys are enabled.
- Passwords are stored as Werkzeug password hashes, not plaintext passwords.
- QR attendance uses expiring server-side tokens and duplicate prevention.
- Existing smart modules remain available.

## Windows — easiest start

1. Open Command Prompt in this folder.
2. Create and activate a virtual environment:

```bat
python -m venv .venv
.venv\Scripts\activate
```

3. Install dependencies:

```bat
python -m pip install -r requirements.txt
```

4. Create an admin key and Flask secret in the environment. For a local demo you can use:

```bat
set FLASK_SECRET_KEY=replace-with-a-long-random-secret
set CAMPUS_ADMIN_KEY=replace-with-your-admin-key
```

5. Initialize the database:

```bat
flask --app app init-db
```

6. Create your first Admin account:

```bat
flask --app app create-user --email admin@example.com --name "Campus Admin" --role Admin
```

The command asks for a password of at least 10 characters.

7. Start the server:

```bat
python app.py
```

8. Open:

`http://127.0.0.1:5000/`

Do not double-click the HTML files. They must be opened through Flask.

## Create campus accounts

After signing in as Admin, use the Admin dashboard to create Student, Faculty and Placement Officer accounts.

The server requires:

- Student: Student ID / USN
- Faculty: Employee ID
- Placement Officer: no fake browser account is accepted; the account must exist in SQLite

## Phone / QR attendance

Run the server on the campus computer using its LAN address and open the LAN URL on the phone. Windows Firewall may need an inbound rule for port 5000.

## Environment variables

See `.env.example` for the supported configuration:

- `FLASK_SECRET_KEY`
- `CAMPUS_ADMIN_KEY`
- `HOST`
- `PORT`
- `CAMPUS_DATABASE`
- `CAMPUS_ALLOWED_NETWORKS`
- `COOKIE_SECURE`

For HTTPS production deployment, set `COOKIE_SECURE=true`.

## Validation

Run the static validation script after installing dependencies:

```bat
python smoke_test.py
```

It checks database initialization, role login, role mismatch rejection, dashboard/API authentication, AI role boundaries and protected file paths.

## Important production upgrades

This is a complete college-project implementation, but it is not a production deployment of a real university. Before public deployment, add:

1. HTTPS behind a production reverse proxy.
2. MFA for Admin and privileged accounts.
3. Persistent rate limiting / login lockout backed by Redis or another shared store.
4. CSRF protection if authentication architecture changes to support cross-site browser workflows.
5. Automated encrypted database backups and restore testing.
6. Centralized structured logging and alerting.
7. Stronger upload validation and malware scanning for uploaded assignment/evidence files.
8. Privacy/retention policies for attendance, location and face-presence data.
9. A production WSGI server rather than Flask's development server.
10. Automated unit/integration tests in CI.
11. Real email/SMS notification providers if required.
12. MFA/recovery flows instead of placeholder “Forgot Password?” links.

## AI assistant scope

The built-in assistant is a deterministic portal-data assistant, not a general-purpose model and not a filesystem reader. It answers only topics permitted for the signed-in role and queries the SQLite records needed for that topic.
