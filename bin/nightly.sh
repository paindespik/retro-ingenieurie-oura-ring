#!/usr/bin/env bash
# oura nightly job — Phase 2
# Exécution : 05:35 (oura-nightly.timer, utilisateur oura).
#
# Dérive les nuits depuis le brut open_oura (/srv/oura/oura.db) :
#   - fenêtres de sommeil (bedtime_period), staging heuristique 30 s
#   - score estimé (anchors publiés + poids officiels Oura)
#   - température nocturne + baselines (EMA asymétrique ecore)
#   - briefing LLM (llama-swap 127.0.0.1:8012) de la dernière nuit
# puis met à jour derived.db lu par le portail oura.example.com.
#
# Logique complète : /srv/oura/bin/derive_night.py
# (source : repo oura-ring, serveur/derive_night.py)

set -u
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

LOG=/srv/oura/logs/nightly.log
mkdir -p /srv/oura/logs

{
  echo "=== $(date '+%F %T') nightly (phase 2) ==="
  if python3 /srv/oura/bin/derive_night.py \
       --db /srv/oura/oura.db \
       --derived /srv/oura/derived.db \
       --briefing 2>&1; then
    echo "nightly OK"
  else
    echo "nightly FAILED (exit $?)"
  fi
} | tee -a "$LOG"
