#!/usr/bin/env bash
# Backup only the V3 pilot database and V3 private configuration. Never V2.
set -euo pipefail
umask 077

readonly container=difedegames-v3-db-1
readonly database=difedegames_v3
readonly backup_root=/home/joe/difedegames-v3-backups
readonly private_dir=/home/joe/.config/difedegames-v3
readonly checkout=/home/joe/difedegames-v3

test "$(id -un)" = joe
test "$(docker inspect "$container" --format '{{index .Config.Labels "com.docker.compose.project"}}')" = difedegames-v3
test "$(docker inspect "$container" --format '{{index .Config.Labels "com.docker.compose.service"}}')" = db
test "$(docker inspect "$container" --format '{{.State.Running}}')" = true
test "$(stat -c %U "$private_dir")" = joe
test "$(stat -c %a "$private_dir")" = 700
for name in database.env runtime.env import.env; do
    test "$(stat -c %a "$private_dir/$name")" = 600
done

if [ -e "$backup_root" ]; then
    test -d "$backup_root"
    test "$(stat -c %U "$backup_root")" = joe
else
    mkdir -m 700 "$backup_root"
fi
chmod 700 "$backup_root"
# A user timer and a manual run must not overlap.
exec 9>"$backup_root/.backup.lock"
flock -n 9 || { echo 'V3 backup already running' >&2; exit 1; }

stamp=$(date -u +%Y%m%dT%H%M%SZ)
pending=$(mktemp -d "$backup_root/.pending-$stamp-XXXXXXXX")
final="$backup_root/$(basename "$pending" | sed 's/^\.pending-/pilot-/')"
trap 'echo "V3 backup failed; private pending files retained at $pending" >&2' ERR

docker exec "$container" pg_dump -U difedegames_v3 -d "$database" --format=custom > "$pending/database.dump.partial"
test -s "$pending/database.dump.partial"
docker exec -i "$container" pg_restore --list < "$pending/database.dump.partial" > "$pending/database-toc.txt"
docker exec -i "$container" pg_restore --file=/dev/null < "$pending/database.dump.partial"
mv "$pending/database.dump.partial" "$pending/database.dump"
tar -czf "$pending/private-config.tar.gz" -C /home/joe/.config difedegames-v3
tar -tzf "$pending/private-config.tar.gz" >/dev/null
{
    printf 'backup_utc=%s\ndatabase=%s\ncontainer=%s\n' "$stamp" "$database" "$container"
    git -C "$checkout" rev-parse HEAD
    docker inspect "$container" --format 'image={{.Image}} started={{.State.StartedAt}}'
    printf 'source_dump_unchanged_and_retained=/home/joe/difedegames-backups/recovery-2026-10-31-THbUhYYR/database.dump\n'
    printf 'database_consistency=pg_dump_transactional_snapshot; private_config_captured_sequentially\n'
} > "$pending/metadata.txt"
(
    cd "$pending"
    sha256sum database.dump database-toc.txt private-config.tar.gz metadata.txt > SHA256SUMS
    sha256sum -c SHA256SUMS >/dev/null
)
find "$pending" -maxdepth 1 -type f -exec chmod 600 {} +
test ! -e "$final"
mv "$pending" "$final"
trap - ERR
printf 'V3 backup verified and published: %s\n' "$final"
