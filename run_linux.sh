#!/usr/bin/env bash
set -e
if [ ! -x ".venv/bin/python" ]; then
  echo "Virtual environment not found. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi
export FLASK_SECRET_KEY="${FLASK_SECRET_KEY:-local-development-secret-change-me}"
export CAMPUS_ADMIN_KEY="${CAMPUS_ADMIN_KEY:-local-development-admin-key-change-me}"
.venv/bin/python app.py
