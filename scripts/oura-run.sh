#!/usr/bin/env bash
# oura-run.sh <arguments oura...>
# Lanceur de commandes Oura avec reconnexion fiable — SANS JAMAIS purger le bond
# (purger cote PC casse definitivement l'appairage cote anneau : reset obligatoire).
#
# Strategie : scan actif (l'anneau doit annoncer), connexion via le bond existant,
# puis execution de la commande dans la fenetre de connexion.
set -uo pipefail
K="${OURA_KEY:-$HOME/.oura/ring.key}"
DB="${OURA_DB:-$HOME/.oura/oura.db}"
ID="${OURA_ID:-XX:XX:XX:XX:XX:XX}"
SCAN=/tmp/oura-run-scan.log
BUDGET="${OURA_BUDGET:-240}"

log() { echo "[$(date +%T)] $*" >&2; }

: > "$SCAN"
timeout $((BUDGET + 20)) bluetoothctl --timeout $((BUDGET + 10)) scan on > "$SCAN" 2>&1 &
SP=$!
trap 'kill $SP 2>/dev/null' EXIT

deadline=$(( $(date +%s) + BUDGET ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  # adresse fraiche annoncee (si non resolue) sinon identite bondee
  addr=$(grep -aiE "Oura " "$SCAN" 2>/dev/null | tail -1 | grep -aoE "[0-9A-F:]{17}" | head -1)
  target="${addr:-$ID}"

  if bluetoothctl info "$ID" 2>/dev/null | grep -q "Connected: yes"; then
    log "deja connecte"
  else
    log "connexion a $target"
    timeout 15 bluetoothctl connect "$target" >/dev/null 2>&1
  fi

  if bluetoothctl info "$ID" 2>/dev/null | grep -q "Connected: yes" \
     || { [ -n "$addr" ] && bluetoothctl info "$addr" 2>/dev/null | grep -q "Connected: yes"; }; then
    log "connecte -> oura $*"
    oura --key-file "$K" --db "$DB" "$@"
    rc=$?
    kill $SP 2>/dev/null
    exit $rc
  fi
  sleep 3
done
log "anneau injoignable (${BUDGET}s)"
exit 1