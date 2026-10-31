# Isolated V3 pilot operations

V2 stays at https://games.difedes.com and must remain running until the user
explicitly requests retirement. V3 is a point-in-time import, not synchronization.
The static preview remains available until the pilot passes acceptance review.

## Exact scope

- Checkout: `/home/joe/difedegames-v3`, branch `codex/v3-preview`.
- Static rollback: `v3/compose.yaml`; pilot: `v3/pilot.compose.yaml`.
- Every command must use `--project-name difedegames-v3` and the explicit V3 file.
- Never use the repository root's legacy Compose file or `--remove-orphans`.
  Static preview and pilot share the V3 project but intentionally use different
  services/networks, so an orphan warning is expected while both are staged.
- Host port: only `127.0.0.1:5003`; never touch V2 port 5002.
- Fresh volume `difedegames-v3_pgdata`; database network `difedegames-v3_data`
  is internal and has no published ports or V2 mounts/connections.
- PG: 512 MB / 0.5 CPU; API: 384 MB / 0.5 CPU; web: 128 MB / 0.25 CPU.
- The user manages Cloudflare's `games2.difedes.com` route. No tunnel edits here.

## Private configuration and restore provenance

Private directory `/home/joe/.config/difedegames-v3` is Joe-owned mode 700; files
are mode 600. A Windows recovery copy is outside Git under
`C:\PROD\_backups\difedegames\2026-10-31\v3-private`, restricted to the current
user, SYSTEM, and Administrators. Never print these env files or effective Compose
configuration with resolved secrets. Validate with `config --quiet` instead.

`scripts/prepare-private.py <private-directory>` generates new configuration only
when all destination files are absent. It refuses overwrites and never prints
credentials. It creates independent cluster, runtime, and source-reader passwords.
The runtime role `difedegames_v3` is not a cluster superuser and cannot create
roles/databases. A new opaque-session cookie name is owned by the V3 API; V2
session credentials are never reused.

Prepared databases:

- `difedegames_v3`: fresh normalized pilot target.
- `difedegames_v3_test`: separate test target; never point test fixtures at pilot.
- `difedegames_v3_legacy_snapshot_20261031`: retained restored legacy source.
  `v3_legacy_reader` has SELECT-only grants and defaults to read-only transactions.

The actual one-time pilot import is now complete (38/38 importer checks passed):
31 people, 9 accounts, 7 groups, 566 sessions, 1,394 seats, 13,543 score cells,
and all 25,595 original source records retained. Do not rerun initialization or
import against this populated target. The separate test database contains only
disposable acceptance fixtures and may be reset by the guarded acceptance runner.

The source was restored from the verified custom-format dump at
`/home/joe/difedegames-backups/recovery-2026-10-31-THbUhYYR/database.dump`.
SHA256: `7784ad3c339969a016eaaac9f014a26489a4126e6297c67e6afced08b978077b`.
Its aggregate reconciliation is 27 public tables / 25,595 rows. The full original
dump remains unchanged and has a private off-server copy. `legacy-schema.sql` is
a schema-only export from this restored source, not a connection to live V2.
This snapshot excludes games played after backup time. Do not silently overwrite
new V3 games with a later import.

## Build and acceptance staging (does not replace the preview)

Build and test the frontend on the development machine (`v3/web`), then package
only its `dist/` directory for transfer to the separate remote V3 checkout.
Do not include `node_modules`, private envs, or production data. The web Dockerfile
copies that static artifact into the pinned existing Nginx image; no server Node
build is needed. Build API and web sequentially; do not build legacy root images.

From `/home/joe/difedegames-v3`, after the reviewed code/artifact is present:

```sh
docker compose --project-name difedegames-v3 -f v3/pilot.compose.yaml config --quiet
docker compose --project-name difedegames-v3 -f v3/pilot.compose.yaml build api
docker compose --project-name difedegames-v3 -f v3/pilot.compose.yaml build web
```

Application validation must use the separate test database (private `test.env`),
and disposable test data must never be inserted into the imported pilot target.
The database container may be provisioned independently while static preview runs:

```sh
docker compose --project-name difedegames-v3 -f v3/pilot.compose.yaml up -d --no-deps --pull never db
```

After backend/import acceptance, run migrations once on the fresh target:

```sh
docker compose --project-name difedegames-v3 -f v3/pilot.compose.yaml run --rm --no-deps migrator < /dev/null
```

Run the one-time importer as a named container so its sanitized report can be
copied before removing that exact container. Its source env is available only to
the importer service, not the serving API. The CLI refuses nonempty targets and
requires the isolated-source confirmation. Do not add options to bypass this.

```sh
docker compose --project-name difedegames-v3 -f v3/pilot.compose.yaml run --name difedegames-v3-initial-import --no-deps importer < /dev/null
docker cp difedegames-v3-initial-import:/tmp/import-report.json /home/joe/.config/difedegames-v3/import-report.json
chmod 600 /home/joe/.config/difedegames-v3/import-report.json
docker rm difedegames-v3-initial-import
```

Inspect aggregate reconciliation before serving. Preserve the report off-server.
Do not run the import again after any pilot use. Follow backend documentation for
additional validation and import preconditions.

## Accepted pilot cutover (V3 port only)

Only after acceptance, stop the V3 static preview and start the pilot services:

```sh
docker compose --project-name difedegames-v3 -f v3/compose.yaml stop preview
docker compose --project-name difedegames-v3 -f v3/pilot.compose.yaml up -d --no-build --pull never api web
curl -fsS http://127.0.0.1:5003/healthz
curl -fsS http://127.0.0.1:5003/api/health
```

Record V2 container IDs, image IDs, start timestamps, and HTTP 5002 before/after;
they must be unchanged. Verify public V3 health, secure-cookie behavior, score
permissions, and the pilot/snapshot notice before inviting use.

## Static preview rollback without deleting data

```sh
docker compose --project-name difedegames-v3 -f v3/pilot.compose.yaml stop web api
docker compose --project-name difedegames-v3 -f v3/compose.yaml up -d --no-build --pull never preview
curl -fsS http://127.0.0.1:5003/healthz
```

Leave the V3 database and its volume intact. No `down -v`, volume removal, V2
commands, or automatic production restoration. Before a later migration, take a
fresh logical V3 backup plus private configuration backup and verify a disposable
restore; preserving the old V2 snapshot is not a backup of new V3 pilot scores.

## Password-recovery SMTP configuration for the parity release

An authorized private mapping is prepared at
`/home/joe/.config/difedegames-v3/smtp.env`, owner Joe, mode 600. It uses the same
existing Gmail provider credentials as V2, with Gmail's supported port 587 and
verified STARTTLS instead of the legacy application's implicit TLS on port 465.
The keys are `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM`, `SMTP_USERNAME`,
`SMTP_PASSWORD`, and `SMTP_STARTTLS`; values remain outside Git and logs.
Only the serving `api` service loads this file at the approved final cutover.
The separate `migrator`, importer, and acceptance containers never receive SMTP
credentials. Do not run test password-reset messages against real recipients.

Once the parity API is deployed with this configuration, production password
recovery can send single-use reset links; administrative reset links remain an
alternative. Configuration work verified only EHLO/STARTTLS/EHLO/QUIT with
certificate verification (TLS 1.3). No authentication or email transmission was
performed, so actual delivery is not claimed as tested. Gmail's port guidance:
https://support.google.com/mail/answer/7104828?hl=en . The private configuration
archive in each scheduled V3 backup includes `smtp.env` automatically.
