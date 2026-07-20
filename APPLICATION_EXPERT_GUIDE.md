# DiFede Games Application Expert Guide

**Purpose:** This is the authoritative reference for AI assistants working on DiFedeAppV2. Read this document at the start of any session. Paste it into a new chat to onboard any model instantly.

**What this app is:** A family game-night score tracker for the DiFede family and allied "crew" families. Users log in, pick players from their family (plus crew allies), start a game session, enter scores round-by-round, and view leaderboards. It is NOT a rules engine or digital board -- it tracks scores only.

**Live environment:** Port **5002**, database **`difedeappv2`**, systemd service **`difedeappv2.service`**. V1 production is port 5001 / database `difedeapp` -- never modify V1.

---

## 1. Tech Stack

| Layer | Technology |
|-------|------------|
| Backend | Flask (Python 3), raw psycopg2 SQL (no ORM in production path) |
| Database | PostgreSQL `difedeappv2` on localhost |
| Real-time | Flask-SocketIO + eventlet (`async_mode='eventlet'`) |
| Frontend | Bootstrap 5.1.3, jQuery, Font Awesome 6, Jinja2 templates |
| Auth | Flask-Session (filesystem), bcrypt/werkzeug passwords |
| Entry | `run.py` -> `create_app()` in `app/__init__.py` |

---

## 2. Core Architecture

```
games (type definition: Five Crowns, UNO, etc.)
    |
    v
active_games (one session = one played game)
    |
    +-- active_game_players (who is playing)
    +-- game_scores (every score entry, saved immediately)
    +-- game_stats (winner record on completion)
```

**Critical distinction:**
- `games.id` = game TYPE (e.g., Five Crowns = 1)
- `active_games.id` = game SESSION (one night of play)
- `/api/scores` uses `active_games.id` as `game_id`
- `/api/games/new` uses `games.id` as `game_id`
- `game_stats.game_id` references `active_games.id` (NOT `games.id`)

**Score persistence:** PostgreSQL is the single source of truth. Every score cell edit POSTs to `/api/scores` immediately. `game_scores` is the unified table. `five_crowns_scores` is legacy read-only fallback for old Five Crowns data only.

**Game numbering:** View `game_sessions_numbered` adds `game_number` (per game type, oldest-first) and `family_game_number` (per family + game type). Use `game_number` everywhere in UI -- never raw session IDs for display.

---

## 3. Multi-Family / Crew System

- Each **user** belongs to one **family** (`users.family_id` -> `families`).
- **Players** on score sheets belong to families (`players.family_id`).
- **Crew** = accepted alliance between families (`family_alliances` where `status='accepted'`).
- Player dropdowns show "Your Family" and "Crew - {family name}" optgroups via `/api/players` and `get_family_players()`.
- Game sessions store `active_games.family_id` and `active_games.user_id` (session owner).
- Only the session owner can update scores (checked in `/api/scores`).

**Roles:** `super_admin`, `family_admin`, `family_member`. Family leads can manage team players and delete family games.

---

## 4. Key Files

| File | Role |
|------|------|
| `run.py` | Starts app on 0.0.0.0:5002 via socketio.run |
| `config.py` | DB creds, session config, secret key |
| `app/__init__.py` | create_app(), blueprints, SocketIO init |
| `app/routes.py` | All game pages, APIs, dashboard, leaderboard (~2900 lines) |
| `app/auth_routes.py` | Login, register, profile, admin under `/auth` |
| `app/auth.py` | `@login_required`, `@admin_required`, session helpers |
| `app/database.py` | `get_db_connection()` with RealDictCursor |
| `app/events.py` | SocketIO handlers + broadcast helpers |
| `app/templates/base.html` | Themes (`--df-*` CSS vars), navbar, AppModal, global CSS |
| `app/templates/dashboard.html` | Active games; needs per-slug icon + Continue links |
| `CREATINGANEWGAME.MD` | Step-by-step checklist for adding a new game |
| `README.md` | Ops guide: service management, backup, troubleshooting |

---

## 5. The `game_page()` Pattern

Central handler in `app/routes.py` (~line 107):

```python
def game_page(slug, game_id):
    # Loads game_def, active/paused/completed sessions, scores, players
    template = slug.replace('-', '_') + '.html'
    return render_template(template, ...)
```

Each game gets a thin route:

```python
@main.route('/sevens')
@login_required
def sevens():
    return game_page('sevens', 10)
```

And a `SLUG_TO_URL` entry (~line 556) so the Games page Play button goes to the score sheet:

```python
SLUG_TO_URL = {
    'five-crowns': '/five-crowns',
    'uno-classic': '/uno',       # slug != URL path
    'uno-flip': '/uno-flip',
    'dutch-blitz': '/dutch-blitz',
    'trouble': '/trouble',
    'basic-other': '/basic-other',
    'kings-corner': '/kings-corner',
    'gin-rummy': '/gin-rummy',
    'sevens': '/sevens',
}
```

**Template variables passed to every game template:**
- `game_def`, `active_game`, `paused_games`, `completed_games`
- `game_players`, `scores` (dict of `(player_id, round_number): score`)
- `players`, `game_family_id`, `user_family_id`

---

## 6. Current Games (as of June 2026)

| ID | Name | Slug | Route | Template | Scoring | Notes |
|----|------|------|-------|----------|---------|-------|
| 1 | Five Crowns | five-crowns | /five-crowns | five_crowns.html | low_wins | Legacy endpoints; 11 rounds |
| 2 | UNO | uno | -- | -- | -- | Variant group parent only |
| 3 | UNO Classic | uno-classic | /uno | uno_classic.html | high_wins | Reference template |
| 4 | UNO Flip | uno-flip | /uno-flip | uno_flip.html | high_wins | Light/dark side toggle |
| 5 | Dutch Blitz | dutch-blitz | /dutch-blitz | dutch_blitz.html | high_wins | Negative scores via toggle |
| 6 | Trouble | trouble | /trouble | trouble.html | high_wins | Single-round SOW; color pick |
| 7 | Basic / Other | basic-other | /basic-other | basic_other.html | configurable | Custom game name; 3 scoring modes |
| 8 | Kings in the Corner | kings-corner | /kings-corner | kings_corner.html | low_wins | Multi-round OR single-round SOW |
| 9 | Gin Rummy | gin-rummy | /gin-rummy | gin_rummy.html | high_wins | Multi-round manual entry |
| 10 | Sevens | sevens | /sevens | sevens.html | low_wins | Multi-round penalty scoring |

---

## 7. Game Scoring Patterns

Most new games use **Pattern A**. Only add custom backend code when Pattern A cannot work.

### Pattern A: Multi-Round Manual Entry (default)
- Round-by-round score table, click cells to edit
- `POST /api/scores` on every entry
- `POST /api/games/complete/<id>` for generic completion with trophy rankings
- Examples: UNO Classic, Gin Rummy, Sevens, Kings Corner (multi-round mode)
- Reference template: `uno_classic.html` or `gin_rummy.html`

### Pattern B: Single-Round with Custom Completion
- Pick winner, enter per-loser details, app calculates score
- Custom `POST /api/games/<slug>/complete` endpoint in routes.py
- Examples: Trouble (SOW from peg counts), Kings Corner (SOW from deadwood)

### Pattern C: Basic / Other (open-ended)
- Custom game name, player count 1-10, three scoring modes
- `custom_game_name` stored on `active_games`
- Dashboard shows "Basic: {name}"
- Leaderboard filterable by custom name within category

---

## 8. Generic API Endpoints

| Endpoint | Method | Payload | Notes |
|----------|--------|---------|-------|
| `/api/games/new` | POST | `{game_id, player_ids, scoring_direction, target_score}` | `game_id` = games.id |
| `/api/scores` | POST | `{game_id, player_id, round_number, score}` | `game_id` = active_games.id; null score deletes |
| `/api/games/pause/<id>` | POST | -- | |
| `/api/games/resume/<id>` | POST | -- | |
| `/api/games/complete/<id>` | POST | -- | Ranks by score sum; writes game_stats; trophy summary |
| `/api/games/delete/<id>` | POST | -- | Owner, family lead, or super_admin |
| `/api/players` | GET/POST | -- | Family + crew players |
| `/api/game-details/<slug>` | GET | -- | Rules modal content |

Custom completion endpoints: `/api/games/trouble/complete`, `/api/games/kings-corner/complete`, `/api/games/basic/complete-quick`

---

## 9. SocketIO

**Client (in game templates):**
```javascript
var sock = io({transports: ['websocket']});
sock.on('connect', function(){ {% if active_game %}sock.emit('join_game', {game_id: {{ active_game.id }}});{% endif %} });
sock.on('score_update', function(d){ /* update cell */ });
sock.on('game_completed', function(d){ /* show modal */ });
```

**CRITICAL Jinja2 pitfall:** Never write `function(){{% if`. The `{{` starts a Jinja variable. Always use a space: `function(){ {% if active_game %}...{% endif %} }`.

**Server broadcasts** (from `app/events.py`): `score_update`, `game_completed`, `game_paused`, `game_resumed` to room `game_{id}`.

---

## 10. Theme System

9 themes in `base.html` via `[data-theme="..."]` selectors. Key CSS variables:

| Variable | Use |
|----------|-----|
| `--df-bg` | Page background |
| `--df-surface` | Dark panels |
| `--df-card` | White card backgrounds |
| `--df-text` | Light text (on dark backgrounds) |
| `--df-text-card` | Dark text (on white/light backgrounds) |
| `--df-accent` | Brand accent |

**Light-on-light bug (recurring):** Dark themes set body to light `--df-text`. Any white/light section MUST explicitly set `color: #212529` or `color: var(--df-text-card)`. Protected classes in base.html: `.new-game-section`, `.score-sheet`, `.completed-games`, `.modal-content`, `.bg-light`, etc. See `.cursor/rules/theme-text-contrast.mdc`.

Game-specific accent colors (burgundy for Gin Rummy, navy for Sevens) are allowed inside the game container but score sheets and form sections must always force dark text.

---

## 11. Dashboard Integration (required for every new game)

Add 6 entries in `app/templates/dashboard.html`:

1. Card view icon (`{% elif game.slug == 'slug' %}`)
2. Card view Continue link
3. Table view icon
4. Table view display (inherits from icon block)
5. Table view Continue link
6. (Card view display name uses `game.game_name` automatically)

Without these, active games show a generic dice icon and link to `/games` instead of the score sheet.

---

## 12. Adding a New Game (summary)

Full checklist: **`CREATINGANEWGAME.MD`**

1. Backup DB if schema changes
2. INSERT into `games` and `game_details`
3. Generate image -> `app/static/images/<slug_underscored>.png`
4. Create `app/templates/<slug_underscored>.html` (copy from `gin_rummy.html` or `uno_classic.html`)
5. Add route + `SLUG_TO_URL` entry in `routes.py`
6. Add dashboard icon + Continue links (6 places)
7. Verify Jinja2 parses: `python3 -c "from jinja2 import Environment, FileSystemLoader; Environment(loader=FileSystemLoader('app/templates')).get_template('slug.html')"`
8. Restart server; verify `lsof -i :5002` shows one process

---

## 13. Server Operations

```bash
# Production (systemd auto-restarts on crash)
sudo systemctl restart difedeappv2.service
sudo journalctl -u difedeappv2.service -f

# Development (stop service first to avoid port conflict)
sudo systemctl stop difedeappv2.service
cd /home/joe/DiFedeAppV2 && source venv/bin/activate && python3 run.py

# Kill stale processes (ALWAYS do before manual restart)
lsof -i :5002 -t | xargs -r kill -9
```

**Why port 5002 auto-restarts:** `difedeappv2.service` has `Restart=always` and `RestartSec=10`.

---

## 14. UX Rules (non-negotiable)

- Mobile-first. Everything must work on iPhone.
- No emojis in code, comments, templates, or UI text.
- Labels must be plain English ("Who Wins?" not "Scoring Direction").
- Every action saves immediately -- never batch scores client-side only.
- Modals outside game container div (z-index stacking).
- Completion popup shows full rankings with trophy for winner (generic endpoint does this).
- Game numbers from `game_sessions_numbered`, not session IDs.

---

## 15. Documents Index

| Document | When to use |
|----------|-------------|
| **APPLICATION_EXPERT_GUIDE.md** (this file) | Full app understanding; paste into new AI sessions |
| **CREATINGANEWGAME.MD** | Adding a new game (includes image creation guide) |
| **README.md** | Service ops, backup, troubleshooting |
| **AI_GAME_CREATOR.md** | Future AI game creator spec (NOT implemented) |
| **.cursorrules** | Always-on coding standards |
| **.cursor/rules/theme-text-contrast.mdc** | Light-on-light text prevention |

---

## 16. Common Pitfalls

| Problem | Cause | Fix |
|---------|-------|-----|
| Internal Server Error on game page | Jinja `function(){{% if` in SocketIO JS | Add space: `function(){ {% if %}` |
| Stale code served | Multiple processes on 5002 | `lsof -i :5002`, kill all, restart |
| Invisible form labels | Light text on white section | `color: #212529` on light backgrounds |
| Play button goes to landing page | Missing `SLUG_TO_URL` entry | Add slug to dict in routes.py |
| Game # shows as session ID | Not using `game_number` from view | Use `game.game_number or game.id` |
| Leaderboard sorts 1,10,11,2 | Alphabetical sort | `data-order` attribute + numeric DataTables type |
| Dashboard Continue goes to /games | Missing slug case in dashboard.html | Add elif for slug in both views |
