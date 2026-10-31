# V3 feature and appearance release — 2026-10-31

Live: https://games2.difedes.com. V2 remains at https://games.difedes.com until Joe explicitly requests retirement. Existing V2 account credentials were copied; V3 requires a fresh sign-in. No real user's password was entered during acceptance.

## Code and runtime

The implementation is in `C:\DEV\DiFedeGamesV3`, branch `codex/v3-preview`, in the existing randjcoding/difedegames repository. The original `C:\PROD\DiFede Games` checkout remains clean on the V2 baseline. Rocky uses `/home/joe/difedegames-v3`, Compose project `difedegames-v3`, and host loopback port 5003. Always use the explicit V3 Compose file in PILOT_DEPLOYMENT.md.

The frontend is React/TypeScript with Vite and React Router. FastAPI, SQLAlchemy, Alembic and a separate PostgreSQL 16 database implement the backend. Same-origin HTTP commands persist scores; authenticated SSE sends committed snapshots. No paid runtime service is required beyond the existing hosting arrangements.

All ten games are available, including both UNO directions, UNO Flip hand sides, signed Dutch Blitz/Skyjo totals, Basic winner-only mode, Kings single-result scoring, and Trouble peg-based Strength of Win. The interface has phone-first round entry, explicit save/conflict states, durable device drafts, completion review, live watching, history filters, guests, groups and private invitation links. New modern artwork includes each game's name; ARTWORK.md records the generation prompts and source paths.

## Restored features and appearance

The home page includes statistics, live games, Hall of Fame and Hall of Shame, recent games, and the latest full score sheet. Leaderboards support all-time and game-specific comparisons with search and filters. Unknown historical winners remain unknown; incompatible scoring systems are not ranked by a single raw-points total.

Five designs (Classic, Arcade, Scoreboard, Clubhouse and Playground) share every feature and can each use nine independent palettes. Classic with Midnight Blue is the default. The Style & color control saves choices per account. See APPEARANCE.md for the shared component/token architecture: new features are implemented once, not separately for each design. Favorites rise to the top of game selection, and reusable game setups retain a roster without silently granting viewing or scorekeeper access.

Account settings support preferred names, contact/privacy settings and password changes. Group leads can manage memberships, invitations and eligible guest profiles. Joe's existing account is the protected site owner and superadmin; the Admin area includes site-wide account/group visibility, audit history and historical score corrections. Corrections require a reason and revision check, preserve original results and completion dates, and record who changed what. Private directory search respects discovery and minor protections. Password recovery is configured, but real email authentication/delivery has not been tested.

Read-only score sheets use compact rows with no inner vertical scrollbar. Wide seven-player sheets scroll horizontally inside the table on a phone; editable round entry retains comfortable touch targets. Phone, landscape and desktop layouts use the same scoring logic and permissions.

## Data preservation and separation

The October 31 V2 snapshot imported 31 people, 9 accounts, 7 groups, 33 memberships, 566 game sessions, 1,394 seats and 13,543 canonical score cells. All 25,595 source rows from 27 tables remain in private provenance. Original account/person links, password hashes, preferred names, privacy flags, score metadata, original winner records and seat order were reconciled.

The import retained 22 score cells found only in the older Five Crowns table. Two historical scores lacked source seating rows; their existing player identities were added to the imported roster and the discrepancy recorded. Two completed sessions had no original result records; no winner was invented. The 564 recorded result rows matched their saved score totals in this snapshot.

V2 and V3 do not synchronize. New games or corrections made in either version remain there. Returning to the V2 address is immediate; it does not move V3 games into V2. A later consolidation needs an explicit reconciliation plan, not another blind full import. The importer refuses nonempty targets.

## Recovery

V3 has a daily private logical backup at approximately 03:20 America/New_York with bounded jitter. Backup files and private configuration are stored under `/home/joe/difedegames-v3-backups`; there is no automatic deletion policy yet. The post-import backup was restored into a disposable isolated PostgreSQL 16 instance, and all 20 V3 public tables matched both row counts and canonical content hashes.

The verified backup and evidence have an off-server, ACL-restricted copy under `C:\PROD\_backups\difedegames\2026-10-31\v3-private`. Subsequent scheduled backups currently remain on Rocky; recurring off-server replication and failure notifications are not configured. The preserved V2 recovery bundle/dump remains separate. Follow PILOT_DEPLOYMENT.md for exact scoped rollback and restore instructions; retain the database when returning V3 to its static preview.

Before this feature release, a fresh live backup was captured at `/home/joe/difedegames-v3-backups/pilot-20261031T141257Z-tD5Ni0sH`, with an off-server copy under `C:\PROD\_backups\difedegames\2026-10-31\pre-parity-20261031T141257Z`. A disposable PostgreSQL restore matched all 20 table counts and content hashes, including the recently completed game 606. Git tag `recovery-v3-pre-parity-20261031T141257Z` and retained API/web images provide a code rollback point. The additive management migration preserved all original domain columns; recovery-image inserts were rehearsed against the upgraded schema. This release does not rerun the legacy import or replace the live database.

## Acceptance evidence

- Feature release: parent reruns passed 80 backend tests, 21 frontend unit tests, strict TypeScript/production build and 80 browser tests. The browser suite renders all 45 style/palette combinations at four viewport sizes. A separate preferred-name/profile regression also passed after the final copy change.
- Independent real HTTP with two Uvicorn workers and PostgreSQL: 23 checks passed, including simultaneous writes, idempotent retries, completion races, permissions, all game modes and open-stream logout revocation.
- Feature-release PostgreSQL rerun: all 23 gameplay checks plus 10 management/privacy checks passed against the final API image in an isolated synthetic database. These cover concurrent historical corrections, password/reset races, partial preference updates, last-lead protection, archive/create races, owner roles, session revocation, directory privacy and saved setup ownership/default uniqueness. The same checks are included in CI.
- The 0004 migration rehearsal retained all 19 original domain-table projections and migrated both preserved saved layouts. Game 606 remained unchanged. The owner bootstrap was verified separately and is idempotent.
- Real source import rehearsal: 14 checks passed. Actual pilot import: all 38 reconciliation checks passed.
- Sixteen actual local API/browser workflows exercised all ten games, additional scoring directions and specialty result forms.
- Public deployment smoke: 13 checks passed using curl. Cloudflare rejected the default Python urllib client with HTTP 403; browser/curl checks reached the application without changing edge security.
- Parent browser review verified the public sign-in page and local mobile sequential entry, zero values, missing-score completion rejection, correction links, dark mode and named artwork.
- V2's five container IDs, images and start timestamps remained unchanged and HTTP returned 200 throughout acceptance.

Browser checks cover 390×844, 375×812, landscape and desktop. They are not a physical iPhone/Safari keyboard test. GitHub Actions now runs V3-specific regression checks without deploying; local validation does not establish a successful hosted Actions run.

## Remaining public-launch work

This is a usable invite-only pilot, not a declaration that the full public-launch product is complete. Physical iPhone 12/13 use, accessibility review and a real family game night remain acceptance gates. Automated tests and backups reduce risk; they do not prove the absence of defects.

Advanced identity merging/transfer approvals, crew/family-alliance management, email ownership verification, public signup, feedback/notification workflows, complete legacy rules content and pause/resume controls remain follow-up work. Preserved legacy crew relationships do not automatically grant visibility to games. Current account, group, analytics and correction workflows are implemented; this is not a claim of complete legacy-feature parity. Production load/security review, operational alerting and recurring off-server backups need deliberate follow-up before broader launch. Trouble board-photo scoring remains a separate feasibility project; the current application uses confirmed manual peg counts.

## Deployment record — 2026-10-31 15:14 UTC

Integration commit `13a6ff3cd82504bd71ddb500e40f5e9bb07edfdd` was deployed to games2.difedes.com. API image: `sha256:841e3523da813926441244a27a963f9748ea226d0403c454b4232b3e4e713200`; web image: `sha256:adf5f6939d35d1e8a76282c7c2efb4a6af8aae6e6c9323efcb2c1ce0ef377877`. The web artifact SHA256 is `41e04b7ddf7f9b999eaa7402bfc46fe5360d359b9813066eccaf91ce4bf59bc6`.

The immediate predeployment backup is `/home/joe/difedegames-v3-backups/pilot-20261031T151130Z-5LM2nhHZ`; all four backup checksums matched the protected off-server copy in `C:\PROD\_backups\difedegames\2026-10-31\deploy-parity-13a6ff3`. The database dump SHA256 is `f9cee90b07a2be6a127727bb25504852846caf4aec6d2756e8b19af47ab6157b`. The earlier pre-parity snapshot received the disposable restore drill; this immediate backup received archive/logical-dump parsing and checksum verification, without another restore drill.

The serving API was paused briefly to capture a consistent baseline and backup. Migration to 0004 preserved all 19 original domain-table projections, including the original audit rows and the full game 606 hash. Owner bootstrap appended one audit event; one requested owner/superadmin and two preserved saved layouts were confirmed. No import or database reset occurred. Only API/web were recreated; V2's five containers and the V3 database retained their IDs, images and start timestamps. Public smoke passed 13/13 plus ten focused deployment checks; all V3 services were healthy and V2 returned HTTP 200.

Existing-provider STARTTLS configuration is loaded only by the serving API for password recovery. TLS negotiation was verified; authentication and actual message delivery were not tested, and no email was sent. Sanitized deployment proofs and matching checksums are in the protected deployment directory's `evidence` subfolder. Retained pre-parity images support code rollback without restoring or replacing the live database.

Parent public-browser acceptance confirmed the deployed sign-in page, Classic/Midnight Blue default, all five design choices and nine palettes, and password-recovery navigation. No real account credentials or reset requests were submitted; the fresh public review tab had no console errors. Authenticated feature acceptance used the isolated synthetic API/browser fixture and the PostgreSQL permission checks described above.
