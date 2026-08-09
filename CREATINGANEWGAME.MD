# Creating a New Game - Complete Guide

This document contains everything needed to add a new game to DiFede Games. Provide this file (or `APPLICATION_EXPERT_GUIDE.md`) to an AI along with the game rules and expectations, and it can bootstrap the game without reviewing the full codebase.

For full application architecture, see **`APPLICATION_EXPERT_GUIDE.md`**.
For live deploy (Rocky), see **`DEPLOY_ROCKY.md`**.

**Last refreshed:** August 2026 (VariableRounds, PlayerPicker, Skyjo, Rocky Docker deploy).

---

## What You Need to Provide

When requesting a new game, provide these details:

- **Game name** (e.g., "Dutch Blitz")
- **Short description** (1-2 sentences)
- **Min/max players** (e.g., 2-8)
- **Scoring direction**: `high_wins` or `low_wins` (or `single_round` / `manual` if Pattern B/C)
- **Target score**: default value (or null if no target), and whether it should be customizable
- **Has rounds/hands?** Yes or No
- **Round labels**: Numbered (1, 2, 3...) or named (3s, 4s, ... K) or "Hand"
- **Auto-add rounds?** Yes for most target/endless games; **No** only for fixed-round games (Five Crowns style)
- **Round view / mobile Table-Round-Full?** Yes for almost all multi-round boards
- **Allow negative scores?** Yes/No
- **Dealer tracking?** Yes or No (extra UI; UNO Classic is the reference if yes)
- **Special board mechanics?** (flip dark-side, blitz formula, partnerships, SOW, etc.)
- **Standalone or variant?** If variant, which parent game group
- **Primary color** (hex, e.g., #006B3F)
- **Secondary/accent color** (hex, e.g., #DAA520)
- **Game image**: "Generate per image guide" or provide a file path
- **Full rules text**
- **Equipment needed**
- **Scoring formula or explanation** (for the point reference panel)
- **Estimated duration, age recommendation, difficulty**
- **Scoring pattern**: Pattern A (multi-round), Pattern B (single-round custom completion), or Pattern C (Basic/Other style)
- **`display_order` hint** (optional): where it should sit on the Games page relative to existing titles

---

## Checklist (Every Step Required)

### 1. Database: `games` table

Backup V2 first (dev DB on Ubuntu):

```bash
PGPASSWORD=Password pg_dump -h localhost -U difedeapp -d difedeappv2 > backup_$(date +%Y%m%d_%H%M%S).sql
```

Get next ID and a sensible display order:

```sql
SELECT MAX(id) FROM games;
SELECT id, name, display_order FROM games ORDER BY display_order, id;
```

```sql
INSERT INTO games (id, name, slug, min_players, max_players, scoring_direction,
    default_target_score, has_rounds, image_url, description, parent_game_id,
    is_variant_group, display_order)
VALUES (<id>, 'Game Name', 'game-slug', 2, 8, 'high_wins', 100, TRUE,
    '/static/images/game_slug.png', 'Short description here.', NULL, FALSE,
    <display_order>);
```

Key columns:
- `slug`: URL-safe lowercase with hyphens (e.g., `dutch-blitz`)
- `scoring_direction`: `'high_wins'` or `'low_wins'` (session can override where the UI allows)
- `default_target_score`: integer or NULL
- `parent_game_id`: NULL for standalone, or FK to parent `games.id` for variants
- `is_variant_group`: TRUE only for parent grouping entries (e.g., "UNO" parent)
- `display_order`: Games page sort key (Five Crowns=10, Skyjo=15, UNO group=20, ...). Leave gaps of 10 when possible.

Apply the same INSERT on Rocky after testing on Ubuntu (or ship via migration script you run in both places). Never modify V1 (`difedeapp` / port 5001).

### 2. Database: `game_details` table

```sql
INSERT INTO game_details (game_id, rules, scoring_system, equipment_needed,
    estimated_duration_minutes, difficulty_level, age_recommendation,
    tips_and_strategies, description_long, notes, game_type, setup_instructions,
    winning_conditions, variations)
VALUES (
    <game_id>,
    '<h5>How to Play</h5><p>Full rules HTML...</p>',
    '<h5>Scoring</h5><p>Scoring explanation...</p>',
    '1 standard deck',
    30, 'Easy', '8+',
    '<ul><li>Strategy tips...</li></ul>',
    'Extended description...',
    'Additional notes...',
    'Card Game',
    '<p>Setup instructions...</p>',
    '<p>Winning conditions...</p>',
    '<ul><li>Variations...</li></ul>'
);
```

Note: `difficulty_level` must be `'Easy'`, `'Medium'`, or `'Hard'` (CHECK constraint).

### 3. Game Image

See **Game Image Creation** section below. Place at `app/static/images/<slug_underscored>.png`.

Set `image_url` in the games table to `/static/images/<slug_underscored>.png`.

### 4. HTML Template

Create `app/templates/<slug_with_underscores>.html`. Mapping is automatic:

```python
template = slug.replace('-', '_') + '.html'  # dutch-blitz -> dutch_blitz.html
```

**The template MUST:**
- Extend `base.html`
- Use `--df-*` CSS variables for theme colors (game-specific accents allowed in container)
- Force dark text (`color: #212529`) on all white/light sections (score sheet, new game form, modals)
- Place ALL modals OUTSIDE the game container div (z-index stacking context issue)
- Use **`PlayerPicker.mount`** via `{% include 'partials/player_picker.html' %}` for seat selection (family + crew + personal crew)
- Create games via `POST /api/games/new` with `{game_id, player_ids, scoring_direction, target_score}`
- Save scores via `POST /api/scores` with `{game_id, player_id, round_number, score}` (`game_id` = `active_games.id`)
- Use generic pause/resume/complete/delete unless Pattern B needs a custom complete endpoint
- Display `Game #{{ game.game_number or game.id }}` in completed lists (link to `main.game_summary`)
- No emojis in UI text
- Bold score cells (match existing boards: `font-weight: 700` on `.score-cell`)

**Pattern A (default multi-round) -- use the shared board, do not reinvent:**

1. Copy **`gin_rummy.html`** or **`sevens.html`** (not UNO Classic -- that has dealer-specific logic).
2. On the active board, set CSS vars and target:
   ```html
   <div class="game-board ..." data-game-id="{{ active_game.id }}"
        data-target-score="{{ active_game.target_score or '' }}"
        style="--vrb-accent:#...;--vrb-accent-dark:#...;--vrb-gold:#...;">
   ```
3. Include shared partials (round entry **outside** the white `.score-sheet`):
   - `{% set round_label = 'Round' %}` then `{% include 'partials/round_entry_ui.html' %}`
   - wrap table in `#defaultTableView.default-table-view`
   - `{% include 'partials/round_view_controls.html' %}` in action buttons
   - `{% include 'partials/fullscreen_score_modal.html' %}` outside the game container
4. Score table must use `.hand-row`, `.score-cell`, `.total-cell` with `data-player` / `data-round`.
5. Wire the board once:
   ```javascript
   var board = DiFedeVariableRounds.create({
     activeGameId: {{ active_game.id }},
     players: GAME_PLAYERS,  // [{id, name}, ...]
     roundLabel: 'Round',    // or 'Hand'
     allowNeg: false,
     landingUrl: '/game/game-slug',  // game landing after pause
     resumeUrl: '{{ url_for("main.game_slug_fn") }}?game_id={{ active_game.id }}',
     accent: '#...',
     accentDark: '#...',
     gold: '#...'
   });
   board.init();
   ```
6. `DiFedeVariableRounds` (loaded from `base.html` via `variable_round_board.js`) provides:
   - live sync with remote row creation (`DiFedeLiveGame.connect`)
   - `ScoreSave` debounced writes + pause flush
   - auto-add next round when the current round is full (skips while target is crossed until user chooses Keep Playing)
   - mobile Table / Round / fullscreen views

**When NOT to use VariableRounds alone:**
- **Fixed rounds** (Five Crowns): keep custom round list; do not auto-add.
- **Dealer / flip / blitz extras**: start from `uno_classic.html`, `uno_flip.html`, or `dutch_blitz.html` (they already have round view + ensureEmptyRow + live sync).
- **Pattern B SOW**: `trouble.html` or Kings single-round section; still wire `DiFedeLiveGame` `onPaused` / `onResumed` / `onCompleted`.
- **Pattern C**: `basic_other.html` (scored mode already uses VariableRounds; manual winner mode does not).

**Reference templates:**
- **Pattern A (multi-round):** `gin_rummy.html` or `sevens.html` (+ shared partials / VariableRounds)
- **Pattern A + extras:** `skyjo.html`, `uno_classic.html`, `dutch_blitz.html`
- **Pattern B (single-round SOW):** `trouble.html` or kings_corner single-round section
- **Pattern C (open-ended):** `basic_other.html`

### 5. Route Alias in `app/routes.py`

Add next to the other game routes (search for `@main.route('/skyjo')`):

```python
@main.route('/game-slug')
@login_required
def game_slug_fn():
    return game_page('game-slug', <games.id>)
```

### 6. SLUG_TO_URL in `app/routes.py`

Add to the `SLUG_TO_URL` dict (search for `SLUG_TO_URL =`):

```python
'game-slug': '/game-slug',
```

Without this, the Games page Play button links to the landing page instead of the score sheet.

Note: slug and URL path can differ (e.g., `'uno-classic': '/uno'`).

Pause redirects often use `/game/<slug>` which hits the **game landing** page (`game_landing`) -- that is intentional.

### 7. Dashboard: `app/templates/dashboard.html`

Add slug cases in these places (card + table):

1. Card view icon (`{% elif game.slug == 'game-slug' %}`)
2. Card view Continue link → `url_for('main.game_slug_fn')`
3. Table view icon
4. Table view Continue link (btn-sm)

Without these, Live Games shows a generic dice icon and Continue goes to `/games`.

### 8. Verify, smoke, deploy

```bash
# Jinja parse
cd /home/joe/DiFedeAppV2 && source venv/bin/activate
python3 -c "from jinja2 import Environment, FileSystemLoader; Environment(loader=FileSystemLoader('app/templates')).get_template('game_slug.html'); print('OK')"

# Dev (Ubuntu): kill stale listeners, restart
lsof -i :5002 -t | xargs -r kill -9
# then: sudo systemctl restart difedeappv2.service   OR   python3 run.py

# Full regression (preferred before push)
python3 tests/smoke_test.py
```

**Ship to Rocky (live):**

```bash
# on Ubuntu after commit
git push origin main

# on Rocky
cd ~/difedegames && git pull
docker compose up -d --build --scale web=2
curl -sS http://127.0.0.1:5002/lb-health   # expect: ok
git log -1 --oneline
```

Always keep `--scale web=2` (or higher). Do not scale db/redis/proxy. See `DEPLOY_ROCKY.md`.

---

## Game Image Creation

The gold standard reference is `app/static/images/basic_other.png`. All new game images match this aesthetic:

- **Background:** Dark/near-black (#1a1a2e), contrast for foreground
- **Style:** Glossy, semi-realistic digital art (high-end mobile game promo)
- **Lighting:** Warm golden glow behind central elements, sparkle/particle effects
- **Palette:** Rich saturated colors against dark background
- **No text in image** -- app overlays game names via HTML

### Prompt Structure (for GenerateImage tool)

1. Style: "A premium game night themed digital illustration"
2. Background: "Dark background (near black, #1a1a2e) with subtle sparkle and warm glow effects"
3. Subject: Game-specific elements (cards, pieces, boards)
4. Arrangement: How elements are positioned
5. Color palette: Dominant colors matching game theme
6. Style match: "Polished, glossy digital art, high-end mobile game promotional art"
7. End with: "No text in the image"

Always pass reference image:
```
reference_image_paths: ["/home/joe/DiFedeAppV2/app/static/images/basic_other.png"]
```

### File Naming

- Path: `app/static/images/<slug_underscored>.png`
- `kings-corner` -> `kings_corner.png`
- DB: `image_url = '/static/images/kings_corner.png'`

### Example Prompt (Kings in the Corner)

> A premium game night themed digital illustration for "Kings in the Corner" card game. Dark background (near black, #1a1a2e) with subtle sparkle and warm glow effects throughout. The centerpiece features four King playing cards arranged in a diamond/corner formation, each slightly fanned outward from the center, with a golden glow emanating from behind them. An ornate golden crown sits above the cards with subtle light rays. Scattered around the kings are face-down playing cards and a few face-up cards showing descending sequences with alternating red and black colors. Rich color palette: deep golds, royal reds, midnight blacks, with touches of emerald green. The style matches a polished, glossy digital art look with detailed textures on the cards, similar to high-end mobile game promotional art. No text in the image.

### Image Checklist

1. Review `basic_other.png` for target style
2. Identify 3-5 visual elements representing the game
3. Write prompt following structure above
4. Generate with `basic_other.png` as reference
5. Copy to `app/static/images/<slug_underscored>.png`
6. Set `image_url` in the games table

### Existing Game Images

| Game | Filename | Key Visual Elements |
|------|----------|-------------------|
| Five Crowns | five_crowns.png | Crown, playing cards |
| Skyjo | skyjo.png / skyjo_box.png | Numbered Skyjo cards, purple accent |
| UNO Classic | uno_classic.png | UNO cards, colorful |
| UNO Flip | uno_flip.png | UNO Flip cards, light/dark sides |
| Dutch Blitz | dutch_blitz.png | Dutch Blitz cards, speed theme |
| Trouble | trouble.png | Pop-O-Matic bubble, colored pegs |
| Basic / Other | basic_other.png | Cards, chips, dice, chess, dominoes, board path |
| Kings in the Corner | kings_corner.png | Four Kings in corners, golden crown |
| Gin Rummy | gin_rummy.png | Fanned cards with melds, stock pile |
| Sevens | sevens.png | Large gold "7", four suit sevens, spade sequences |

---

## API Reference (Generic -- No New Backend for Pattern A)

| Endpoint | Method | Purpose | Payload |
|----------|--------|---------|---------|
| `/api/games/new` | POST | Start a new game session | `{game_id, player_ids, scoring_direction, target_score}` |
| `/api/scores` | POST | Save/update a score | `{game_id, player_id, round_number, score}` (score=null to delete) |
| `/api/games/pause/<id>` | POST | Pause a game | (none) |
| `/api/games/resume/<id>` | POST | Resume a paused game | (none) |
| `/api/games/complete/<id>` | POST | Complete game, determine winner | (none) |
| `/api/games/delete/<id>` | POST | Delete an active game | (none) |
| `/api/players` | GET | Get players (family + crew + personal crew) | (none) |
| `/api/players` | POST | Create a new player | `{first_name, last_name, display_name}` |
| `/api/game-details/<slug>` | GET | Get rules for Rules modal | (none) |
| `/api/games/<id>/live-scores` | GET | Poll scores for live sync fallback | (none) |

**Payload note:** In `/api/scores`, `game_id` = `active_games.id`. In `/api/games/new`, `game_id` = `games.id`.

### Custom Completion Endpoints (Pattern B only)

| Endpoint | Game | Purpose |
|----------|------|---------|
| `/api/games/trouble/complete` | Trouble | SOW from peg counts |
| `/api/games/kings-corner/complete` | Kings Corner | SOW from loser deadwood |
| `/api/games/basic/complete-quick` | Basic/Other | Winner-only, no round scores |

Add a new custom endpoint in `routes.py` only when generic completion cannot calculate the winner/score.

---

## Template Structure (Pattern A)

```
{% extends "base.html" %}
{% block title %}GAME NAME{% endblock %}
{% block content %}
<div class="SLUG-container">
  {% if not active_game %}
    <!-- New Game: paused list, completed list, PlayerPicker, target, Start -->
  {% else %}
    <!-- Active: header, round_entry_ui, score table, round_view_controls, actions, ref panel -->
  {% endif %}
</div>
<!-- Modals + fullscreen_score_modal OUTSIDE container -->
{% endblock %}

{% block styles %} ... game theme CSS, mobile @media, bold .score-cell ... {% endblock %}
{% block scripts %}
  PlayerPicker.mount(...);
  DiFedeVariableRounds.create({...}).init();  // Pattern A
{% endblock %}
```

Score cells: `class="score-cell" data-player="X" data-round="Y"`
Total cells: `class="total-cell" data-player="X"`
Round rows: `class="hand-row" data-round="N"`

Shared assets (already in `base.html`):
- `app/static/js/live_game.js` → `DiFedeLiveGame`
- `app/static/js/variable_round_board.js` → `DiFedeVariableRounds`
- `ScoreSave` helper on `window` (debounced `/api/scores`)

---

## Live Sync / SocketIO

Prefer **`DiFedeLiveGame.connect(activeGameId, handlers)`** (or VariableRounds, which calls it). Do not hand-roll `io()` unless extending a specialty board.

| Event / handler | When |
|-----------------|------|
| `onScore` / `applyRemoteScore` | Another user updated a score (must create missing rows) |
| `onCompleted` | Another user completed the game |
| `onPaused` / `onResumed` | Another user paused or resumed |
| Pause button | Flush open edits via `ScoreSave.flushAsync` before pause API |

**Jinja2 in SocketIO (CRITICAL):**
```javascript
// WRONG - causes Internal Server Error:
sock.on("connect",function(){{% if active_game %}sock.emit(...);{% endif %}});

// CORRECT - space between { and {%:
sock.on("connect",function(){ {% if active_game %}sock.emit(...);{% endif %} });
```

---

## Theme Rules

- Game containers may use hardcoded accent colors
- White/light sections MUST use `color: #212529` or `color: var(--df-text-card)`
- Protected classes in base.html: `.new-game-section`, `.score-sheet`, `.completed-games`, `.modal-content`, `.bg-light`, `.df-light-bg`
- See `.cursor/rules/theme-text-contrast.mdc` for full details

---

## Current Games in the System

| ID | Name | Slug | Route | Scoring | Target | Type / notes |
|----|------|------|-------|---------|--------|--------------|
| 1 | Five Crowns | five-crowns | /five-crowns | low_wins | NULL | Fixed rounds 3-K; custom board |
| 2 | UNO | uno | -- | -- | -- | Variant group |
| 3 | UNO Classic | uno-classic | /uno | high_wins | 500 | Dealer highlight + VariableRounds-style sync |
| 4 | UNO Flip | uno-flip | /uno-flip | high_wins | 500 | Light/dark side per hand |
| 5 | Dutch Blitz | dutch-blitz | /dutch-blitz | high_wins | 75 | Negatives + leader |
| 6 | Trouble | trouble | /trouble | high_wins | NULL | Pattern B SOW / pegs |
| 7 | Basic / Other | basic-other | /basic-other | configurable | NULL | Pattern C |
| 8 | Kings in the Corner | kings-corner | /kings-corner | low_wins / single_round | NULL | Multi (VRB) or single SOW |
| 9 | Gin Rummy | gin-rummy | /gin-rummy | high_wins | 100 | **Pattern A reference** |
| 10 | Sevens | sevens | /sevens | low_wins | 100 | Pattern A reference |
| 11 | Skyjo | skyjo | /skyjo | low_wins | 100 | Pattern A + neg scores |

---

## The `game_page()` Function

Central handler in `routes.py` (search for `def game_page`). Takes slug and `games.id`:

1. Loads game definition from `games` table
2. Resolves active session via `?game_id=` or most recent incomplete
3. Loads paused games, last completed (with `game_sessions_numbered` for `game_number`)
4. If active: loads players + scores from `game_scores` (fallback `five_crowns_scores` for Five Crowns only)
5. Loads selectable players via family + crew helpers used by PlayerPicker / `/api/players`
6. Renders `slug.replace('-', '_') + '.html'`

**Template variables:**
- `game_def`, `active_game`, `paused_games`, `completed_games`
- `game_players`, `scores`, `players`, `game_family_id`, `user_family_id`

---

## Environments

| Env | Host | Port / URL | Database | How to restart |
|-----|------|------------|----------|----------------|
| Dev (Ubuntu) | build machine | `0.0.0.0:5002` | `difedeappv2` | kill port 5002 + systemd or `run.py` |
| Live (Rocky) | `joe@192.168.68.71` `~/difedegames` | proxy → `127.0.0.1:5002` / https://games.difedes.com | Docker Postgres | `git pull` + `docker compose up -d --build --scale web=2` |

V1 (`difedeapp` / port 5001) is sacred -- never modify.

---

## Known Pitfalls

| Problem | Fix |
|---------|-----|
| Internal Server Error on game page | Check Jinja `function(){{% if` -> add space |
| Invisible text on forms/score sheets | Add `color: #212529` on light backgrounds |
| Play button goes to landing page | Add slug to `SLUG_TO_URL` |
| Dashboard Continue goes to /games | Add slug cases in dashboard.html |
| Stale code after changes (Ubuntu) | Kill all port 5002 processes before restart |
| Live still on old code | Rocky pull + rebuild with `--scale web=2`; confirm `git log -1` and `/lb-health` |
| Live sync drops remote rounds | Must create missing `.hand-row`s (VariableRounds/`applyRemote*`) |
| Round entry looks wrong on desktop | Keep `#roundEntryView` outside white `.score-sheet`; toggle only on mobile |
| Game # shows wrong order | Use `game_number` from view, sort numerically in DataTables |
| StrReplace fails on dashboard.html | File may have CRLF; run `sed -i 's/\r$//' dashboard.html` |
| Copied UNO as Pattern A base | Prefer Gin/Sevens + VariableRounds unless you need dealer/flip/blitz |
| Smoke test score-count fails | Disposable board-check games must delete `game_scores` / session rows after checks |
