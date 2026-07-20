# DiFede Games - Family Game Night Tracker

A web application for tracking scores across family game nights. Supports ten game types (Five Crowns, UNO Classic/Flip, Dutch Blitz, Trouble, Basic/Other, Kings in the Corner, Gin Rummy, Sevens) with real-time multiplayer score updates, leaderboards, and a family/crew alliance system.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Service Management](#service-management)
3. [Tech Stack](#tech-stack)
4. [File Structure](#file-structure)
5. [Database](#database)
6. [Games in the System](#games-in-the-system)
7. [Adding a New Game](#adding-a-new-game)
8. [Theme System](#theme-system)
9. [Multi-Family System](#multi-family-system)
10. [Email Verification](#email-verification)
11. [API Reference](#api-reference)
12. [Server Administration](#server-administration)
13. [Troubleshooting](#troubleshooting)
14. [Architecture Notes](#architecture-notes)
15. [Key Reference Files](#key-reference-files)

---

## Quick Start

```bash
# Start the application
cd /home/joe/DiFedeAppV2 && python3 run.py

# Or use the systemd service
sudo systemctl start difedeappv2.service
```

The app runs on **port 5002** and binds to `0.0.0.0` (accessible from all network devices).

- **URL**: `http://<server-ip>:5002`
- **Admin account**: `joe_71@yahoo.com` (super_admin)
- **Database**: PostgreSQL `difedeappv2` on localhost

---

## Service Management

The application runs as a **systemd service** (`difedeappv2.service`) with `Restart=always` and `RestartSec=10`. This means if you kill the process, systemd will automatically restart it after 10 seconds.

### Start / Stop / Restart

```bash
sudo systemctl start difedeappv2.service
sudo systemctl stop difedeappv2.service
sudo systemctl restart difedeappv2.service
```

### Check Status

```bash
sudo systemctl status difedeappv2.service
```

### Enable / Disable Auto-Start on Boot

```bash
sudo systemctl enable difedeappv2.service    # Start on boot
sudo systemctl disable difedeappv2.service   # Don't start on boot
```

### Development Mode (Manual Start)

When developing, stop the service first so it doesn't conflict:

```bash
sudo systemctl stop difedeappv2.service
cd /home/joe/DiFedeAppV2 && python3 run.py
```

### Kill Stale Processes

Eventlet (the async server) forks workers. When stopped improperly, orphan processes can serve old code on port 5002:

```bash
# Kill everything on port 5002
pkill -9 -f "python.*run.py"; pkill -9 -f "python.*DiFedeAppV2"; sleep 2

# Verify port is clear
lsof -i :5002

# If still occupied, force kill by PID
lsof -i :5002 | grep LISTEN | awk '{print $2}' | xargs -r kill -9
```

After a clean start, `lsof -i :5002 | grep LISTEN` should show only one or two entries from the same process (eventlet parent + worker).

### Service File Location

```
/etc/systemd/system/difedeappv2.service
```

After editing the service file:
```bash
sudo systemctl daemon-reload
sudo systemctl restart difedeappv2.service
```

### Viewing Logs

```bash
# Real-time log stream
sudo journalctl -u difedeappv2.service -f

# Last 50 entries
sudo journalctl -u difedeappv2.service -n 50

# Logs from today
sudo journalctl -u difedeappv2.service --since today

# Filter for errors
sudo journalctl -u difedeappv2.service | grep -i error
```

---

## Tech Stack

| Component | Technology | Details |
|-----------|-----------|---------|
| Backend | Flask (Python 3) | Runs on port 5002 |
| Database | PostgreSQL | DB: `difedeappv2`, User: `difedeapp`, Password: `Password` |
| Real-time | Flask-SocketIO + eventlet | WebSocket for live score updates |
| Frontend | Bootstrap 5.1.3, jQuery, Font Awesome 6 | Jinja2 templates (no React/Vue) |
| Sessions | Flask-Session (server-side filesystem) | |
| Passwords | bcrypt (via werkzeug) | |
| Email | smtplib | Gmail SMTP for notifications |

---

## File Structure

```
DiFedeAppV2/
  run.py              -- Entry point. Binds to 0.0.0.0:5002
  config.py           -- DB credentials, Flask config, secret key
  app/
    __init__.py        -- create_app(), registers blueprints
    routes.py          -- Main blueprint: game pages, APIs, dashboard, admin
    auth_routes.py     -- Auth blueprint: login, register, profile, admin users
    auth.py            -- Auth utilities (email validation, decorators)
    database.py        -- get_db_connection(), execute_query(), execute_modify()
    email_utils.py     -- send_verification_email(), send_alliance_*_email()
    events.py          -- SocketIO event handlers (broadcast_score_update, etc.)
    static/images/     -- Game images (dutch_blitz.png, etc.)
    templates/
      base.html        -- Master template: navbar, themes, global CSS, modals
      dashboard.html   -- User dashboard: live games, crew, stats
      games.html       -- Game browser: cards for each game type
      game_landing.html -- Per-game landing: stats, active games, new game form
      five_crowns.html -- Five Crowns scoresheet
      uno_classic.html -- UNO Classic scoresheet
      uno_flip.html    -- UNO Flip scoresheet (light/dark side toggle)
      dutch_blitz.html -- Dutch Blitz scoresheet
      leaderboard.html -- Cross-game statistics
      my_team.html     -- Family lead dashboard: manage players, view alliances
      family_page.html -- Public family profile page
      admin.html       -- Super admin console
      auth/            -- Login, register, profile, admin users, password reset
```

---

## Database

### Connection

```bash
# Connect to the V2 database
PGPASSWORD=Password psql -h localhost -U difedeapp -d difedeappv2

# Quick check
PGPASSWORD=Password psql -h localhost -U difedeapp -d difedeappv2 -c "SELECT version();"
```

### Key Tables

| Table | Purpose |
|-------|---------|
| `games` | Game type definitions (Five Crowns, UNO, Dutch Blitz, etc.) |
| `game_details` | Extended game info (rules, tips, equipment, scoring) |
| `active_games` | Game sessions (active, paused, completed) |
| `active_game_players` | Players in each game session |
| `game_scores` | All score entries (unified table for all games) |
| `five_crowns_scores` | Legacy score table (read-only fallback for old data) |
| `game_stats` | Winner tracking per completed game |
| `players` | All players across all families |
| `users` | Login accounts |
| `families` | Family groups |
| `family_alliances` | Crew Up system (inter-family connections) |
| `notifications` | In-app notification system |
| `game_sessions_numbered` | **View** - Adds per-game-type sequential numbering to completed games |

### The `game_sessions_numbered` View

Provides sequential game numbers partitioned by game type:

```sql
-- Example output:
-- id=347, game_name='Five Crowns', game_number=1, family_game_number=1
-- id=348, game_name='UNO Classic', game_number=1, family_game_number=1
-- id=350, game_name='Five Crowns', game_number=2, family_game_number=2
```

Columns added by the view:
- `game_number`: Sequential number within the game type (Five Crowns #1, #2, #3...)
- `family_game_number`: Sequential number within game type + family
- `total_games_of_type`: Total times this game has been played globally
- `family_total_of_type`: Total times this family played this game

### Backup and Restore

```bash
# Backup
PGPASSWORD=Password pg_dump -h localhost -U difedeapp -d difedeappv2 > backup_$(date +%Y%m%d_%H%M%S).sql

# Restore
PGPASSWORD=Password psql -h localhost -U difedeapp -d difedeappv2 < backup_file.sql

# Check database size
sudo -u postgres psql -d difedeappv2 -c "SELECT pg_size_pretty(pg_database_size('difedeappv2'));"
```

### PostgreSQL Service

```bash
sudo systemctl status postgresql
sudo systemctl start postgresql
sudo systemctl restart postgresql
sudo systemctl is-enabled postgresql   # Should be "enabled"
```

### V1 vs V2

| | V1 (DiFedeApp) | V2 (DiFedeAppV2) |
|--|---------------|-----------------|
| Port | 5001 | 5002 |
| Database | `difedeapp` | `difedeappv2` |
| Status | Production (source of truth) | Development (all new features) |
| Service | `difedeapp.service` | `difedeappv2.service` |

V1 data has been synced to V2. V1 should NEVER be modified.

---

## Games in the System

| ID | Name | Slug | Scoring | Target | Type |
|----|------|------|---------|--------|------|
| 1 | Five Crowns | five-crowns | low_wins | None (11 fixed rounds) | Standalone |
| 2 | UNO | uno | -- | -- | Variant group (parent) |
| 3 | UNO Classic | uno-classic | high_wins | 500 (configurable) | Variant of UNO |
| 4 | UNO Flip | uno-flip | high_wins | 500 (configurable) | Variant of UNO |
| 5 | Dutch Blitz | dutch-blitz | high_wins | 75 (configurable) | Standalone |
| 6 | Trouble | trouble | high_wins | None (single-round SOW) | Standalone |
| 7 | Basic / Other | basic-other | configurable | None | Standalone (custom names) |
| 8 | Kings in the Corner | kings-corner | low_wins | None | Standalone (multi or SOW) |
| 9 | Gin Rummy | gin-rummy | high_wins | 100 (configurable) | Standalone |
| 10 | Sevens | sevens | low_wins | 100 (configurable) | Standalone |

---

## Adding a New Game

See **`CREATINGANEWGAME.MD`** for the complete step-by-step checklist (includes image creation guide). For full app architecture, see **`APPLICATION_EXPERT_GUIDE.md`**.

In summary:

1. INSERT into `games` table (name, slug, min/max players, scoring direction, target)
2. INSERT into `game_details` table (rules, equipment, tips)
3. Generate game image -> `app/static/images/<slug_underscored>.png`
4. Create template at `app/templates/<slug_underscored>.html` (use `gin_rummy.html` or `uno_classic.html` as reference)
5. Add route alias in `app/routes.py`
6. Add to `SLUG_TO_URL` dict in `app/routes.py`
7. Add dashboard icon + Continue links in `dashboard.html` (6 slug cases)
8. Verify Jinja2 template parses; restart the server

No new backend code is needed for standard multi-round games (Pattern A) -- all APIs are generic.

---

## Theme System

9 themes defined in `base.html` as CSS custom properties on `[data-theme="..."]` selectors.

### Key Variables

| Variable | Purpose |
|----------|---------|
| `--df-bg` | Page background |
| `--df-surface` | Section/panel backgrounds |
| `--df-card` | Card backgrounds (always light) |
| `--df-text` | Body text (readable on bg/surface) |
| `--df-text-card` | Card text (readable on white) |
| `--df-accent` | Accent/brand color |
| `--df-muted` | Secondary/muted text |

### Contrast Rules

- Elements on `--df-surface` (dark) background must use `--df-text` (light)
- Elements on `--df-card` (white) background must use `--df-text-card` (dark)
- NEVER mix these -- wrong combination = invisible text
- Game containers use hardcoded accent colors but must use guaranteed-visible text
- Form inputs globally styled: white background + dark text
- Modals default to white background with dark text (set globally in base.html)
- Custom dark-background modals need explicit `color: #e0e0e0` override

### Available Themes

1. DiFede Dark (default), 2. DiFede Light, 3. Midnight Blue, 4. Espresso, 5. Emerald Forest, 6. Rose Wine, 7. Sunset Blaze, 8. Arctic Frost, 9. Royal Purple

---

## Multi-Family System

### Roles

- **super_admin** (`joe_71@yahoo.com`): Full access. Can move players, approve users, manage all data.
- **family_admin** (family leads): Add/edit/remove players in their family. Access "My Team" dashboard.
- **member**: Play games, view their family's players and crewed-up families.

### Crew Up (Alliances)

Families can send "Crew Up" requests to other families. Once accepted:
- Both families see each other's players in game setup
- Allied players appear in a separate "Crew" optgroup in player dropdowns
- Statistics track across family boundaries

---

## Email Verification

New user registration flow:
1. User registers at `/auth/register`
2. Account created with `is_verified = FALSE`
3. Verification email sent (24-hour expiry)
4. Admin notification sent to `joe_71@yahoo.com`
5. User clicks verification link -> account activated
6. Welcome email sent

Routes:
- `/auth/verify/<token>` -- Handle email verification
- `/auth/resend-verification` -- Resend verification email

Email config: Gmail SMTP via `TheDiFedeApp_games@gmail.com`

---

## API Reference

All game-related endpoints are generic and work for any game type:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/games/new` | POST | Start a new game session |
| `/api/scores` | POST | Save/update a single score |
| `/api/games/pause/<id>` | POST | Pause a game |
| `/api/games/resume/<id>` | POST | Resume a paused game |
| `/api/games/complete/<id>` | POST | Complete game, determine winner |
| `/api/games/delete/<id>` | POST | Delete an active game |
| `/api/players` | GET | Get players (family + crew) |
| `/api/players` | POST | Create a new player |
| `/api/game-details/<slug>` | GET | Get game rules |
| `/api/games/<id>/round-by-round` | GET | Round-by-round scores for leaderboard |
| `/api/games/<id>/final-scores` | GET | Final score totals |

---

## Server Administration

### Firewall

```bash
sudo ufw status                    # Check status
sudo ufw allow 5002/tcp           # Open app port
sudo ufw reload                   # Apply changes
```

### Check Open Ports

```bash
sudo ss -tulpn | grep LISTEN
```

### Virtual Environment

```bash
source /home/joe/DiFedeAppV2/venv/bin/activate
pip install -r requirements.txt
pip list                           # Check installed packages
deactivate
```

### System Health

```bash
uptime                             # System uptime
df -h                              # Disk space
free -h                            # Memory usage
ps aux | grep python               # Running Python processes
```

### Network

```bash
hostname -I                        # Server IP address
curl http://localhost:5002          # Test local access
sudo ss -tulpn | grep 5002         # Check port binding
```

---

## Troubleshooting

### App Won't Start

1. Check service status: `sudo systemctl status difedeappv2.service`
2. Check logs: `sudo journalctl -u difedeappv2.service -n 100`
3. Check if port is in use: `sudo lsof -i :5002`
4. Test database: `PGPASSWORD=Password psql -h localhost -U difedeapp -d difedeappv2 -c "SELECT 1;"`
5. Kill stale processes and restart

### Can't Access from Other Devices

1. Verify firewall allows port 5002: `sudo ufw status`
2. Verify binding on 0.0.0.0: `sudo ss -tulpn | grep 5002` (should show `0.0.0.0:5002`)
3. Check server IP: `hostname -I`
4. Test locally first: `curl http://localhost:5002`

### Stale Process Problem (Recurring)

Eventlet forks workers. When the server is killed incorrectly, the worker can become orphaned (PPID=1) and keep serving old code. Always check `lsof -i :5002` and kill orphans before restarting.

### Light Text on Light Backgrounds (RECURRING)

Dark themes set `body { color: var(--df-text) }` to a light color (e.g. #e0e0e0). Any section with a white or light background that does NOT explicitly set dark text will be invisible. This is the single most common UI bug in this project.

**Global fixes in base.html** cover: `.new-game-section`, `.score-sheet`, `.completed-games`, `.paused-games`, `.game-details`, `.list-group-item`, `.score-cell`, `.bg-light`, `.modal-content`. Use the `.df-light-bg` utility class for custom elements.

**Rule for new code:** Every element with `background: white`, `#fff`, `#f8f9fa`, or similar MUST set `color: #212529` or `color: var(--df-text-card)`. See `.cursor/rules/theme-text-contrast.mdc` for the full Cursor rule.

### Modal Text Readability

Modals default to white backgrounds, but body text inherits `--df-text` from the active theme (light color on dark themes). This is fixed globally in `base.html` with forced dark text on `.modal-content`. If a new modal uses a custom dark background, add an explicit light text override in base.html.

---

## Architecture Notes

For the complete architecture reference (all tables, APIs, scoring patterns, pitfalls, current games), see **`APPLICATION_EXPERT_GUIDE.md`**.

### How Game Pages Work

The `game_page()` function in `routes.py` is the central handler for ALL game types:
- Loads game definition from `games` table
- Finds the user's most recent non-complete game (active or paused, auto-resumes paused)
- Loads paused games, completed games with sequential game numbers, players with scores
- Scores load from `game_scores` table (falls back to `five_crowns_scores` for legacy data)
- Renders the game-specific template matching the slug

### Score Persistence

- All scores save via `POST /api/scores` on every cell edit (no "save" button)
- SocketIO broadcasts updates to all connected clients in the same game room
- Scores persist in the `game_scores` table with `active_game_id`, `player_id`, `round_number`, `score`

### Real-time Updates

Flask-SocketIO with eventlet provides WebSocket communication:
- Clients join game rooms via `join_game` event
- Score changes broadcast to all room members
- Game state changes (pause, resume, complete) broadcast to all room members

### Anthropic API Integration (Future)

An Anthropic API key exists for future AI-powered game creation. See `AI_GAME_CREATOR.md` for the implementation spec.

---

## Key Reference Files

| File | Purpose |
|------|---------|
| `APPLICATION_EXPERT_GUIDE.md` | **Primary AI reference** -- full architecture, all games, APIs, pitfalls; paste into new sessions |
| `CREATINGANEWGAME.MD` | Step-by-step new game creation checklist (includes image creation guide) |
| `README.md` | This file - ops guide, service management, troubleshooting |
| `AI_GAME_CREATOR.md` | Future AI game creator specification (NOT implemented) |
| `.cursorrules` | AI development rules (no emojis, UX philosophy, coding standards) |
| `.cursor/rules/difede-app-expert.mdc` | Always-on Cursor rule with expert context summary |
| `.cursor/rules/theme-text-contrast.mdc` | Light-on-light text prevention rules |

---

## User Accounts

| Email | Role | Family |
|-------|------|--------|
| joe_71@yahoo.com | super_admin | Joe DiFede family (id: 1) |

19 players total across 2 families with 1 active alliance.

---

*Last Updated: March 2026*
