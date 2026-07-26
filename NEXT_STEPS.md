# DiFede Games - Next Steps (Development Backlog)

Status as of 2026-07-26. The app is stable and running on port 5002. Everything
listed below is a **known, open item** - none of it breaks current gameplay, but
these are the next things to do when development resumes.

> Backups before this work: `backup_pre_rebuild_*.sql` (pre identity rebuild) and
> `backup_post_rebuild_*.sql` (verified post-rebuild seed for Rocky) in the project
> root. Always `pg_dump` before schema/data changes.

---

## Multi-family identity rebuild - DONE 2026-07-26 (do NOT redo)

Commit `ba1c7ec` on main. Migration `migrations/004_identity_rebuild.sql` applied.
End-to-end suite: `venv/bin/python tests/smoke_test.py` (69 checks, all passing,
self-cleaning). Includes: privacy-aware directory (`/directory`), minor protection
(visible only to direct allied crews), email invitations + claim-by-email flow,
lead-approved batch transfers, archive/reinstate/purge lifecycle (no hard deletes),
FK hardening (RESTRICT/SET NULL on all score history), lead-scoped duplicate merge,
audit logging, per-family vs lifetime stats split, `/player/<id>` profiles.

### Rocky reseed (run ON the Rocky box - no SSH key from Ubuntu)

```bash
cd ~/difedegames && git pull
# Pull the live V2 data straight from Ubuntu over the LAN (pg_hba already allows it):
PGPASSWORD=Password pg_dump -h 192.168.68.72 -U difedeapp difedeappv2 > deploy/initdb/01_restore.sql
# Wipe the DB volume and restore fresh (see DEPLOY_ROCKY.md "Wipe everything"):
docker compose down && docker volume rm difedegames_dbdata && docker compose up -d --build
curl -s -o /dev/null -w "web: %{http_code}\n" http://localhost:5002/
```

---

## Already done (for reference - do NOT redo)

- Added **Skyjo** (game id 11, second in the list) with box art, full rules, custom target score.
- **Clear History** now scoped to the current user + that game type (was wiping ALL families' history).
- **Player delete** requires a lead role and refuses to delete players with recorded game history.
- **Five Crowns** complete/pause/resume/complete-by-id are owner-scoped.
- Login required on `final-scores`, `round-by-round`, `game-details`; participant validation on score + completion endpoints.
- Scoring direction respected in admin score-edit, final scores, and round-by-round.
- `update_score` runs in a single real transaction; Five Crowns stores `low_wins`.
- DB creds env-overridable; emojis removed; Dutch Blitz blocks completion until all scores entered.

---

## Priority 1 - Security (do first)

- [ ] **Wire up CSRF protection.** `WTF_CSRF_ENABLED = True` is set in `config.py` but no
      `CSRFProtect` is installed. Add Flask-WTF `CSRFProtect`, expose the token, and send it
      with every AJAX/`$.ajax` call (a global `$.ajaxSetup` header is the least-churn approach).
      This touches every game template - plan for it.
- [ ] **`/auth/admin/verify-user/<id>` changes state via GET.** Convert to POST (CSRF-protected).
      A logged-in super-admin could be tricked into a one-click action via a crafted link.
- [ ] **SocketIO has no auth and `cors_allowed_origins="*"`** (`app/__init__.py`, `app/events.py`).
      Require a logged-in session to `join_game`, and lock CORS to the real origin. Also remove the
      **duplicate `connect`/`disconnect` handlers** (defined in both `routes.py` and `events.py`).
- [ ] **Set a real `SECRET_KEY`** via environment variable in production (currently falls back to a
      known dev key in `config.py`).
- [x] ~~Registration self-attach by family name~~ Fixed in the identity rebuild: plain signups
      always get their own new family; joining others goes through the directory + lead approval.
- [x] ~~Tighten alliance permissions~~ Done 2026-07-26: alliance create/accept/decline are
      lead-only (API 403 + dashboard hides the buttons for non-leads). Super admin role grants
      are reserved for the site owner (`OWNER_EMAIL`, default joe_71@yahoo.com) via
      `/auth/admin/users/<id>/set-role` with UI on the User Management page.
- [ ] **Family-view permissions**: viewing `/family/<id>` is open to any logged-in user.
      Decide the intended policy (allied-only?) and enforce it if desired.

## Priority 2 - Correctness / stats

- [ ] **Win-streak stats are computed globally**, not per player (`leaderboard()` ~line 1898).
      The streak numbers are currently misleading - rework to per-player streaks.
- [ ] **`complete_game_generic` omits players who never had a score row** - a blank/abandoned
      game drops those players from the rankings. Rank all participants, not just those with scores.
- [ ] **Hardcoded email verification link** points at a specific LAN IP (`app/auth_routes.py` ~line 213).
      Build it from the request host / a configured base URL.

## Priority 3 - Maintainability

- [ ] **Hardcoded game IDs/slugs** scattered around (`BASIC_GAME_ID = 7`, `game_page('slug', N)`,
      dashboard slug cases). Centralize so adding a game is one place, not many.
- [ ] **`/api/games` admin view misses crew-only games** and admin delete only matches by creator.
- [ ] **Bare `except` blocks** in `execute_query` swallow SQL errors and return `[]` (they look like
      empty results). Log them.
- [ ] **Heavy duplicated JS** across the ~10 game templates (clear-history, delete, complete, modals,
      socket setup). Extract a shared `game_common.js` so future fixes happen once.
- [ ] **Per-game "Clear History" still shares the `/api/games/five-crowns/clear-history` URL.**
      It's now correctly scoped, but the path name is misleading - consider a neutral route name.

---

## If something looks broken while playing

Likely-suspect areas given recent changes:
- Score entry / completion (transaction + participant-validation changes in `update_score`,
  `complete_*` endpoints in `app/routes.py`).
- Clear History buttons (now POST a `game_id` body from each template).
- Skyjo page specifically (`app/templates/skyjo.html`) - newest template.
- Dutch Blitz "Complete" guard (won't complete until every score box is filled).

Quick checks:
- `lsof -i :5002` then `sudo systemctl restart difedeappv2.service` (systemd auto-restarts on crash).
- Service logs: `journalctl -u difedeappv2.service --since "10 min ago"`.
- Jinja parse check: load any edited template via `jinja2.Environment(...).get_template(...)`.
