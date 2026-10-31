# V3 daily backup and isolated recovery

This job backs up only `difedegames_v3` inside the exact V3 container
`difedegames-v3-db-1`, after checking its Compose project/service labels.
It never connects to V2. No backups are automatically deleted.

Daily at 03:20 America/New_York plus up to 15 minutes of jitter, Joe's user timer
runs `scripts/backup-pilot.sh`. Persistent catch-up runs after missed schedules.
User lingering is enabled so this timer can run without an active SSH login.
Inspect scheduling and failures with:

```sh
systemctl --user list-timers difedegames-v3-backup.timer
systemctl --user status difedegames-v3-backup.service
journalctl --user -u difedegames-v3-backup.service --no-pager -n 30
```

Install/update only these two V3 unit files, then reload/enable this timer:

```sh
mkdir -p /home/joe/.config/systemd/user
install -m 600 v3/scripts/difedegames-v3-backup.service /home/joe/.config/systemd/user/
install -m 600 v3/scripts/difedegames-v3-backup.timer /home/joe/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now difedegames-v3-backup.timer
```

## Backup contents and protection

Published runs are private directories under `/home/joe/difedegames-v3-backups`,
mode 700 with files mode 600. Each contains a custom-format `database.dump`, its
TOC, a private V3 configuration archive, metadata, and `SHA256SUMS`. Credentials
and personal data stay out of logs and Git. A lock prevents concurrent runs.
The script fail-fast checks the dump by listing and fully parsing it, checks the
configuration archive and all checksums, then atomically renames the complete
pending directory to its published name. Failed `.pending-*` files are retained
privately for diagnosis and are not complete backups.

Manual run: `systemctl --user start difedegames-v3-backup.service`.
Copy completed runs off-server into the existing ACL-protected Windows backup
area and verify every manifest hash there. A daily on-server timer alone does not
provide off-server disaster recovery or guarantee a successful run: inspect its
status, free disk space, and restore evidence. No retention cleanup is configured.

The original restored V2 snapshot/dump stays separate and unchanged; it cannot
recover scores created later in V3. Before migrations, take an extra V3 backup.

## Restore drill or recovery: fresh isolated destination only

Never restore in place over V2 or the active V3 pilot. First verify all manifest
hashes, then use an unused, newly named PostgreSQL 16 container/volume with
`--network none`, no published ports, and no production mounts. Use the pinned
local PostgreSQL image digest recorded in deployment configuration. Initialize a
fresh `difedegames_v3` role/database and restore with:

```sh
docker exec -i <exact-new-disposable-container> pg_restore \
  -U difedegames_v3 -d difedegames_v3 --exit-on-error < <completed-backup>/database.dump
```

For a no-network disposable cluster only, trust authentication can simplify the
drill; never use it for serving runtime. Restore database ownership normally and
create any additional owner roles listed in the dump before restoring. Compare
schema/table and aggregate row counts, migrations, imported provenance counts,
and application invariants in the restored copy; do not print personal records.
Record image, backup hash, counts, and outcome. Remove only the exact disposable
container and its exact newly created volume, and verify cleanup.

Extract the private configuration archive only into another fresh mode-700
staging directory. Never print its contents or overwrite active configuration.
An actual recovery requires separately reviewed application/configuration
validation and a controlled cutover; it is not an automatic rollback.

## Current verification boundary

The V2 source dump has passed a full disposable restore. The new V3 target was
initially empty. A full disposable V3 restore must be recorded after the actual
pilot import; dump parsing alone is not a restore test. The timer preserves
database state and current V3 private configuration sequentially, not one atomic
cross-component snapshot. It does not export application images, host settings,
Cloudflare credentials, or V2 data.
