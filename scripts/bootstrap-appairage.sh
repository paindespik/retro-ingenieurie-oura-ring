#!/usr/bin/env bash
# TOUT-EN-UN : a lancer juste apres un factory reset (anneau keyless, LED bleue).
# Dans UNE seule fenetre de connexion :
#   purge bonds -> scan continu -> connect RPA fraiche -> bond SMP (agent)
#   -> installation de la cle -> activation HR/SpO2 -> sleep-analyze -> premier sync
LOG=/tmp/oura_full.log
K="$HOME/.oura/ring.key"
DB="$HOME/.oura/oura.db"
: > "$LOG"
echo "=== TOUT-EN-UN $(date +%T) ===" >> "$LOG"

for old in $(bluetoothctl devices 2>/dev/null | grep -i oura | awk '{print $2}'); do
  echo "purge $old" >> "$LOG"; bluetoothctl remove "$old" >/dev/null 2>&1
done

: > /tmp/bt_full.log
timeout 900 bluetoothctl --timeout 890 scan on > /tmp/bt_full.log 2>&1 &
SP=$!
trap 'kill $SP 2>/dev/null' EXIT

last=0
for cycle in $(seq 1 900); do
  addr=$(grep -aiE "Oura " /tmp/bt_full.log 2>/dev/null | tail -1 | grep -aoE "[0-9A-F:]{17}" | head -1)
  now=$(date +%s)
  [ -z "$addr" ] && { sleep 0.5; continue; }
  [ $((now - last)) -lt 6 ] && { sleep 0.5; continue; }
  last=$now

  echo "[$(date +%T)] annonce $addr -> connect" >> "$LOG"
  timeout 15 bluetoothctl connect "$addr" >/dev/null 2>&1
  bluetoothctl info "$addr" 2>/dev/null | grep -q "Connected: yes" || { echo "  connect KO" >> "$LOG"; continue; }

  echo "[$(date +%T)] connecte -> bond SMP" >> "$LOG"
  timeout 25 bluetoothctl pair "$addr" 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | grep -aE "Pairing successful|Failed to pair" >> "$LOG"

  echo "[$(date +%T)] 1/5 installation de la cle" >> "$LOG"
  timeout 70 oura --key-file "$K" --scan-timeout 10 pair >> "$LOG" 2>&1 || { echo "  cle KO, retry" >> "$LOG"; continue; }

  echo "[$(date +%T)] 2/5 activation HR + SpO2" >> "$LOG"
  timeout 70 oura --key-file "$K" features --enable-hr --enable-spo2 >> "$LOG" 2>&1

  echo "[$(date +%T)] 3/5 sleep-analyze" >> "$LOG"
  timeout 90 oura --key-file "$K" --db "$DB" sleep-analyze --force >> "$LOG" 2>&1

  echo "[$(date +%T)] 4/5 premier sync" >> "$LOG"
  timeout 300 oura --key-file "$K" --db "$DB" sync >> "$LOG" 2>&1

  echo "[$(date +%T)] 5/5 inventaire des evenements" >> "$LOG"
  oura --db "$DB" events >> "$LOG" 2>&1

  echo "[$(date +%T)] ===== TERMINE =====" >> "$LOG"
  kill $SP 2>/dev/null
  exit 0
done
echo "[$(date +%T)] echec: anneau jamais joignable" >> "$LOG"