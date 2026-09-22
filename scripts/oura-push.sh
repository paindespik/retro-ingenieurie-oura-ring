#!/usr/bin/env bash
# ~/.local/bin/oura-push.sh — appelé par oura-sync.service après "oura sync"
set -uo pipefail
cd "$HOME/.oura" || exit 1
rm -f inbox-snapshot.db            # VACUUM INTO refuse d'écraser un fichier existant
sqlite3 oura.db "VACUUM INTO 'inbox-snapshot.db'" || exit 0
rsync -a inbox-snapshot.db serv:/srv/oura/inbox/
rsync -a profile.json feature_modes.json score_params.json serv:/srv/oura/inbox/ 2>/dev/null || true
