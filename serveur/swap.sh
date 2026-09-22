#!/usr/bin/env bash
# /srv/oura/bin/swap.sh — merge du snapshot Arch → oura.db live.
# Appelé par oura-swap.timer (15 min). flock contre exécutions concurrentes.
#
# v2 (2026-09-22) : le téléphone pousse ses événements directement dans
# oura.db (POST /ingest/events). Le remplacement complet du DB cassait la
# coexistence (compte de lignes) → on merge : INSERT OR IGNORE (dédup sur
# UNIQUE(serial, tag, ring_timestamp, body)) + max() pour sync_state.
set -uo pipefail
exec 9>/srv/oura/.lock
flock -n 9 || exit 0

S=/srv/oura/inbox/inbox-snapshot.db
L=/srv/oura/oura.db

[ -f "$S" ] || exit 0
sqlite3 "$S" "PRAGMA integrity_check" | grep -q ok || { echo "swap: intégrité KO, snapshot ignoré" >&2; exit 1; }

if [ ! -f "$L" ]; then
  # premier DB : le snapshot devient le live
  mv -f "$S" "$L"
  echo "swap: premier DB ($L) créé depuis le snapshot ($(date -Is))"
else
  out=$(sqlite3 "$L" "
    ATTACH '$S' AS s;
    -- événements : dédup sur la contrainte UNIQUE
    INSERT OR IGNORE INTO main.events
      (serial, tag, name, ring_timestamp, body, decoded_json, captured_unix)
    SELECT serial, tag, name, ring_timestamp, body, decoded_json, captured_unix
    FROM s.events;
    SELECT 'events ' || changes();
    -- readings : pas de UNIQUE dans le schéma → dédup manuel (lignes identiques)
    INSERT INTO main.readings (serial, kind, value, unit, captured_unix)
    SELECT r.serial, r.kind, r.value, r.unit, r.captured_unix
    FROM s.readings r
    WHERE NOT EXISTS (SELECT 1 FROM main.readings m
                      WHERE m.serial = r.serial AND m.kind = r.kind
                        AND m.captured_unix = r.captured_unix AND m.value = r.value);
    SELECT 'readings ' || changes();
    -- device : dernière info gagne (1 ligne par serial)
    INSERT OR REPLACE INTO main.device
      (serial, hardware_id, firmware, api_version, mac, updated_unix)
    SELECT serial, hardware_id, firmware, api_version, mac, updated_unix
    FROM s.device;
    SELECT 'device ' || changes();
    -- sync_state : le curseur le plus avancé gagne
    UPDATE main.sync_state SET
      next_cursor = max(next_cursor,
        (SELECT next_cursor FROM s.sync_state ss WHERE ss.serial = main.sync_state.serial)),
      last_sync_unix = max(last_sync_unix,
        (SELECT last_sync_unix FROM s.sync_state ss WHERE ss.serial = main.sync_state.serial))
    WHERE serial IN (SELECT serial FROM s.sync_state);
    INSERT OR IGNORE INTO main.sync_state
      (serial, next_cursor, last_sync_unix)
    SELECT serial, next_cursor, last_sync_unix
    FROM s.sync_state
    WHERE serial NOT IN (SELECT serial FROM main.sync_state);
    SELECT 'sync_state ' || changes();
  " 2>&1)
  rc=$?
  rm -f "$S"
  if [ $rc -ne 0 ]; then
    echo "swap: merge KO : $out" >&2
    exit 1
  fi
  echo "swap: merge terminé ($(date -Is)) — $(echo "$out" | tr '\n' ' ')"
fi
