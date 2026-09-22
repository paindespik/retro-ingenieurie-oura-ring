#!/usr/bin/env bash
cd "$HOME/oura-ring/serveur"
export OURA_RAW_DB="$HOME/oura-ring/test/oura.db"
export OURA_DERIVED_DB="$HOME/oura-ring/test/derived.db"
export OURA_INBOX="$HOME/oura-ring/test"
exec "$HOME/oura-ring/venv/bin/python" -m uvicorn oura_web:app --host 127.0.0.1 --port 8095
