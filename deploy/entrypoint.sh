#!/usr/bin/env bash
set -e

host="${PG_HOST:-db}"
port="${PG_PORT:-5432}"

echo "Waiting for database at ${host}:${port} ..."
for i in $(seq 1 60); do
  if (echo > "/dev/tcp/${host}/${port}") >/dev/null 2>&1; then
    echo "Database is reachable."
    break
  fi
  if [ "$i" -eq 60 ]; then
    echo "ERROR: database not reachable after 120s" >&2
    exit 1
  fi
  sleep 2
done

exec "$@"
