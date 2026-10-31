# V3 API pilot

This is independent of the root Flask application. It never imports V2 configuration.
`DATABASE_URL` is mandatory. Runtime is PostgreSQL 16 with SQLAlchemy 2 and Alembic.

## Development and tests

From `v3/api`, create a virtual environment and install `requirements.lock`.
The lock includes test tools so the same reviewed dependency set can run acceptance checks.
`requirements.txt` records allowed source ranges; regenerate and review the lock deliberately.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.lock
.\.venv\Scripts\python -m pytest -q
```

These tests use fresh temporary SQLite databases and no real user data. PostgreSQL locking,
stream behavior over a real socket, and restored-snapshot reconciliation require the separate
`v3/acceptance` harness and isolated V3 test database. SQLite results do not establish PG concurrency.

For synthetic local browser QA only, set `DATABASE_URL=sqlite:///dev-pilot.db`,
`APP_BASE_URL=http://localhost:5173`, `ALLOWED_ORIGINS=http://localhost:5173`, and
`SESSION_COOKIE_SECURE=false`. Then run `alembic upgrade head`,
`python -m difede_api.dev_seed`, and `uvicorn difede_api.main:app --host 127.0.0.1 --port 8000 --no-access-log`.
The seed refuses PostgreSQL/nonempty accounts. It creates only synthetic `person1@example.com`
through `person3@example.com`, password `ExamplePass!123`; person 3 is a watcher.
Never seed the imported pilot database.

## Isolated PostgreSQL runtime

- `DATABASE_URL=postgresql+psycopg://.../difedegames_v3`
- `APP_BASE_URL=https://games2.difedes.com`
- `ALLOWED_ORIGINS=https://games2.difedes.com`
- `SESSION_COOKIE_SECURE=true`

Run `alembic upgrade head` before starting `uvicorn difede_api.main:app --host 0.0.0.0 --port 8000 --no-access-log`.
The API belongs on a private Compose network behind the same-origin proxy, without a published port.
Only that proxy may set trusted forwarded headers. No V2 credentials or volumes belong in runtime.

Authentication uses fresh opaque hashed database sessions, with HttpOnly host-only Secure Lax
cookies. Send the `/api/me` token as `X-CSRF-Token` on authenticated mutations.
Login and anonymous invite acceptance reject cross-site origins; there is no open registration.
Invites are single-use group-admin-issued links, not proof of an arbitrary supplied email inbox.
Old action tokens/password reset links are provenance only and never activated in V3.
Access logs must omit invite tokens; the image disables API access logging.

## Snapshot import

First restore the backed-up V2 database into a distinctly named isolated PostgreSQL source,
such as `difedegames_v3_legacy_snapshot_20261031`. Give a dedicated reader only SELECT rights
and `default_transaction_read_only=on`. Keep the complete dump separately backed up.
Provide `LEGACY_SOURCE_URL` only to the one-off importer, not API runtime. Provide the separate
empty target's `DATABASE_URL`; migrations must already be applied.

```sh
python -m difede_api.import_legacy \
  --source-name difedegames_v3_legacy_snapshot_20261031 \
  --confirm-isolated --report /tmp/import-report.json
```

The CLI checks source naming and target isolation, opens a repeatable-read/read-only source
transaction, and refuses every occupied target. A failed import rolls back target changes.
It copies all source tables to immutable private provenance, maps people/accounts/groups/sessions
by their original IDs, preserves password hashes and original winner records, and resets target
sequences. It never matches people by names or recalculates old winners. Unified score cells win
over duplicate legacy Five Crowns cells; legacy-only cells remain readable fallback.

The JSON report contains counts, source-table fingerprints, explicit reconciliation checks, and
anomaly counts. It contains no names, addresses, password hashes, or active links. Raw provenance
contains sensitive data and has no public API. Imported paused flags and legacy-only structures
remain in provenance because the V3 pilot contract has no pause state or legacy administrative UI.

Rollback means retaining V2 and the original snapshot, not overwriting V2. Once V3 accepts writes,
restoring an older V3 snapshot loses those later writes; back up V3 before maintenance.

## Consistency and streams

Commands lock one game session row. Cell versions protect individual edits; game revisions
protect finish/reopen/round commands. Command idempotency, score/state changes, audit records,
and durable events share one transaction. Clearing preserves a versioned null tombstone.
Targets are prompts, never automatic completion. Normal round completion requires all required
saved cells; trailing wholly empty variable rounds are excluded. Early completion is incomplete
and awards no official win. Managers reopen or soft-delete; players/assigned keepers score.

SSE returns full committed snapshots, checks current session and access every two seconds, emits
heartbeats, and closes with `access_revoked` after revocation. Every reconnect starts with a current
authorized snapshot; clients keep draft/base versions for conflict reconciliation. No broker or
URL bearer token is needed. Dashboard/history cards bulk-load summaries without round/cell data;
`GET /api/games` accepts `status`, `slug`, `q`, `limit` (1–100), and `offset`, returning `games,total`.

## Additive management upgrade

Existing V3 databases use `alembic upgrade head`; never rerun the strict-empty legacy importer.
Migration 0003 adds account site roles, owner capability, account preferences/private profile
copies, person details, group privacy settings and hashed action tokens. It backfills privileges
only from matching immutable imported user records. Migration 0004 adds saved game setups and
copies preserved legacy layouts by original ID and player order. Neither migration rewrites
existing score cells, game results, account/person links or immutable imported records.

After reviewing the migrated role counts, an operator may run
`python -m difede_api.bootstrap_owner` with `OWNER_EMAIL` supplied from private operator
configuration. The matched account must already be an active approved verified legacy superadmin.
Runtime authorization uses the persisted capability, never an email allowlist. No email or
password is printed by the bootstrap. Rollback retains the additive database schema and uses
previous API/web images; destructive migration downgrade is disabled. New column defaults remain
in the database so previous images can still create rows.

Management, profile, preferences/favorites, directory and saved-setup routes are documented in
`v3/PARITY_API.md`. Site admins can inspect all people/groups/games. Group leads manage their own
memberships and manageable sole-group guests; another group's canonical identity/account stays
under that person's/site-admin control. Leadership transfers, deactivation and membership removal
protect the last active group lead and site owner. Historical corrections require a reason and
saved revision, keep completed-game dates stable, and retain original imported results separately.

Password changes and successful reset consumption revoke actual V3 sessions. Manual administrator
reset links always work without email delivery. User-requested reset delivery is optional and only
activated when `SMTP_HOST` and `SMTP_FROM` are configured; `SMTP_PORT` defaults to 587,
`SMTP_STARTTLS=true` uses verified TLS, and `SMTP_USERNAME`/`SMTP_PASSWORD` stay in private runtime
configuration. Without transport, requests return an honest generic contact-admin response and do
not invalidate existing manual reset links. No successful-delivery claim is fabricated.

Advanced identity merges/releases, active crew/alliance management, public registration and
verification/resend, feedback threads, notifications, complete legacy rules content and pause
workflows remain outside this bounded management phase. Their original records remain preserved.
Anonymizing an active profile retains sensitive immutable provenance and is not a GDPR erasure
guarantee. Physical phone acceptance and deployment backup exercises remain separate release gates.
