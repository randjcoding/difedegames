#!/usr/bin/env bash
set -euo pipefail
HBA=/etc/postgresql/16/main/pg_hba.conf
MARKER="# DiFede Games LAN access (Rocky + local)"

if [[ $EUID -ne 0 ]]; then
  echo "Run with: sudo bash $0"
  exit 1
fi

if grep -Fq "$MARKER" "$HBA"; then
  echo "LAN entries already present — leaving $HBA unchanged"
else
  cat >> "$HBA" <<'RULES'

# DiFede Games LAN access (Rocky + local)
host    difedeappv2    difedeapp    192.168.68.71/32    scram-sha-256
host    difedeappv2    difedeapp    192.168.68.0/22     scram-sha-256
host    difedeappv2    difedeapp    192.168.68.71/32    md5
host    difedeappv2    difedeapp    192.168.68.0/22     md5
RULES
  echo "Appended LAN rules to $HBA"
fi

systemctl reload postgresql
echo "PostgreSQL reloaded."
echo
echo "--- last 15 lines of pg_hba.conf ---"
tail -n 15 "$HBA"
echo
echo "Test from this box via LAN IP:"
PGPASSWORD=Password psql "postgresql://difedeapp:Password@192.168.68.72:5432/difedeappv2" -c "SELECT COUNT(*) AS active_games FROM active_games;"
