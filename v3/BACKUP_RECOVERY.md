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

## Recorded post-import restore verification

After the actual pilot import, backup
`pilot-20261031T132745Z-SNCuFd5d` passed a full disposable PostgreSQL 16 restore
on 2026-10-31 at 13:28 UTC. Every public table's count and canonical row-hash
aggregate matched the original V3 target: all 20 tables. The isolated restore
used network none, no published ports, and a fresh volume; its exact container
and volume were removed and absence verified. Dump SHA256:
`00fdf0eb8ebbfb53e5984b1ee261d95e1347eca689cbc97a4e8e6701732ebbb3`.

Private report: `/home/joe/.config/difedegames-v3/pilot-restore-drill.txt`, with
before/restored fingerprint files beside it. The complete backup was copied to
the ACL-protected Windows `v3-private/backups/` folder and all four manifest
hashes verified. Sanitized acceptance reports and V2 before/after identity
evidence are in the sibling Windows `v3-private/evidence-20261031/` folder.
This validates this backup; future scheduled backups still require monitoring
and periodic restore drills. The timer preserves
database state and current V3 private configuration sequentially, not one atomic
cross-component snapshot. It does not export application images, host settings,
Cloudflare credentials, or V2 data.
