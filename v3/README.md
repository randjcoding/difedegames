# DiFede Games V3 pilot

The pilot is live at https://games2.difedes.com. Current V2 games remain available
at https://games.difedes.com and keep running until the user explicitly requests
retirement. The two environments have separate databases, sessions, secrets,
containers, and storage. New scores do not synchronize between them.

## What is available

React scorekeeping frontend, same-origin FastAPI API, and isolated PostgreSQL 16.
All ten game types are supported, including Five Crowns' fixed hands, flexible
round games, Trouble pegs, Kings single results, and Basic winner-only games.
Guests, groups, explicit invitations, participant/nonplaying scorekeeper access,
read-only watching, durable save retries, conflict handling, and saved history
are included. Existing imported account password hashes are preserved; no V2
session cookies are accepted. Invitations do not send email.

The imported point-in-time history contains 31 people, 9 accounts, 7 groups,
566 sessions, 1,394 seats, and 13,543 score cells. All 25,595 original source
records and original recorded results are retained as provenance. The isolated
source database and original dump remain preserved separately. Do not import
again into the populated pilot or replace scores created after launch.

## Hosting and exact command scope

Development worktree: `C:\DEV\DiFedeGamesV3`, branch `codex/v3-preview`.
Server checkout: `/home/joe/difedegames-v3`.
Host bind: only `127.0.0.1:5003`; PostgreSQL and API have no published host ports.
The user configured Cloudflare's `games2.difedes.com` route. V2 port 5002 is separate.

From the server V3 checkout:

```sh
docker compose --project-name difedegames-v3 -f v3/pilot.compose.yaml ps
docker compose --project-name difedegames-v3 -f v3/pilot.compose.yaml config --quiet
curl -fsS http://127.0.0.1:5003/api/health
```

Always specify this project and V3 file. Never use the root legacy Compose file,
`--remove-orphans`, or volume-deleting commands for V3 operations. Private env
files are outside Git in `/home/joe/.config/difedegames-v3`, mode 700/600; do not
print resolved Compose configuration or credentials.

## Recovery and retained static preview

See [PILOT_DEPLOYMENT.md](PILOT_DEPLOYMENT.md) for staged builds, import boundaries,
and a static-preview rollback that keeps the database. See
[BACKUP_RECOVERY.md](BACKUP_RECOVERY.md) for the daily 03:20 New York backup timer,
checksum verification, and disposable restore procedure. The timer has no
retention deletion and does not automatically transfer future backups off-server.

Post-import backup `/home/joe/difedegames-v3-backups/pilot-20261031T132745Z-SNCuFd5d`
passed a complete isolated restore with all 20 public table counts and canonical
row-hash aggregates matching. A verified off-server copy and acceptance evidence
are in `C:\PROD\_backups\difedegames\2026-10-31\v3-private` with restricted ACLs.

The former static preview remains under `v3/preview` and `v3/compose.yaml`.
To restore that preview without deleting pilot data:

```sh
docker compose --project-name difedegames-v3 -f v3/pilot.compose.yaml stop web api
docker compose --project-name difedegames-v3 -f v3/compose.yaml up -d --no-build --pull never preview
```

## Validation and pilot limits

Independent real HTTP/two-worker PostgreSQL acceptance passed 23 checks,
including concurrent writes, retries, lifecycle races, role boundaries, guest
claims, live stream logout revocation, all game modes, and secure cookie headers.
Snapshot import rehearsal passed 14 independent checks; actual import passed all
38 reconciliations. Backend and frontend test evidence is recorded separately.
Anonymous operational checks cover routing, security headers, API/database
health, static 404/cache behavior, and access denial without creating real-user
sessions. V2 container identities, images, and start timestamps are preserved.
Public cutover passed all 13 operational checks on 2026-10-31 at 13:33 UTC using
the actual curl transport. Accepted application sources: API `05e6f79`, frontend
`e70fd4a` (mobile sign-in improvement deployed with only the web container
recreated). Deployment reports are private files `deployed-smoke.json` and
`deployed-runtime.txt` beside the V3 private configuration, with off-server copies.
The final web-only update passed another 13 public checks; its report is
`deployed-mobile-web.txt`. API, database, and all V2 container identities and
start times remained unchanged across that update.

This is an invite-only pilot with manually entered scores. Physical iPhone/Safari
validation, larger-load testing, password recovery/email delivery, and automatic
photo scoring are not completed. Historic house-rule outcomes are preserved;
new rule notes distinguish published alternatives and custom scoring. Keep using
V2 whenever its established workflow is needed during the tandem period.
