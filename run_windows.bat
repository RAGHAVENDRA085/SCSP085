@echo off
setlocal
if not exist ".venv\Scripts\python.exe" (
  echo Virtual environment not found.
  echo Run: python -m venv .venv
  echo Then: .venv\Scripts\activate
  echo Then: python -m pip install -r requirements.txt
  exit /b 1
)
if "%FLASK_SECRET_KEY%"=="" set FLASK_SECRET_KEY=local-development-secret-change-me
if "%CAMPUS_ADMIN_KEY%"=="" set CAMPUS_ADMIN_KEY=local-development-admin-key-change-me
.venv\Scripts\python.exe app.py
