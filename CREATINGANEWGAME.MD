# Creating a New Game - Complete Guide

This document contains everything needed to add a new game to DiFede Games. Provide this file (or `APPLICATION_EXPERT_GUIDE.md`) to an AI along with the game rules, and it can build the game without reviewing the full codebase.

For full application architecture, see **`APPLICATION_EXPERT_GUIDE.md`**.

---

## What You Need to Provide

When requesting a new game, provide these details:

- **Game name** (e.g., "Dutch Blitz")
- **Short description** (1-2 sentences)
- **Min/max players** (e.g., 2-8)
- **Scoring direction**: `high_wins` or `low_wins`
- **Target score**: default value (or null if no target), and whether it should be customizable
- **Has rounds/hands?** Yes or No
- **Round labels**: Numbered (1, 2, 3...) or named (3s, 4s, ... K)
- **Dealer tracking?** Yes or No
- **Standalone or variant?** If variant, which parent game group
- **Primary color** (hex, e.g., #006B3F)
- **Secondary/accent color** (hex, e.g., #DAA520)
- **Game image**: "Generate per image guide" or provide a file path
- **Full rules text**
- **Equipment needed**
- **Scoring formula or explanation** (for the point reference panel)
- **Estimated duration, age recommendation, difficulty**
- **Scoring pattern**: Pattern A (multi-round manual), Pattern B (single-round custom completion), or Pattern C (Basic/Other style)

---

## Checklist (Every Step Required)

### 1. Database: `games` table

Get next ID: `SELECT MAX(id) FROM games;`

```sql
INSERT INTO games (id, name, slug, min_players, max_players, scoring_direction,
    default_target_score, has_rounds, image_url, description, parent_game_id, is_variant_group)
VALUES (<id>, 'Game Name', 'game-slug', 2, 8, 'high_wins', 100, TRUE,
    '/static/images/game_slug.png', 'Short description here.', NULL, FALSE);
```

Key columns:
- `slug`: URL-safe lowercase with hyphens (e.g., `dutch-blitz`)
- `scoring_direction`: `'high_wins'` or `'low_wins'`
- `default_target_score`: integer or NULL
- `parent_game_id`: NULL for standalone, or FK to parent `games.id` for variants
- `is_variant_group`: TRUE only for parent grouping entries (e.g., "UNO" parent)

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

**Always backup before schema changes:**
```bash
PGPASSWORD=Password pg_dump -h localhost -U difedeapp -d difedeappv2 > backup_$(date +%Y%m%d_%H%M%S).sql
```

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
- Save scores via `POST /api/scores` with `{game_id, player_id, round_number, score}`
- Create new games via `POST /api/games/new` with `{game_id, player_ids, scoring_direction, target_score}`
- Use generic endpoints for pause/resume/complete/delete (unless Pattern B custom completion)
- Connect to SocketIO with correct Jinja spacing (see Pitfalls)
- Use `<optgroup>` labels for "Your Family" and "Crew" in player dropdowns
- Display `Game #{{ game.game_number or game.id }}` in completed games lists

**Reference templates:**
- **Pattern A (multi-round):** `gin_rummy.html` or `uno_classic.html`
- **Pattern B (single-round SOW):** `trouble.html` or kings_corner.html single-round section
- **Pattern C (open-ended):** `basic_other.html`

### 5. Route Alias in `app/routes.py`

Add after existing game routes (~line 745):

```python
@main.route('/game-slug')
@login_required
def game_slug_fn():
    return game_page('game-slug', <games.id>)
```

### 6. SLUG_TO_URL in `app/routes.py`

Add to `SLUG_TO_URL` dict (~line 556):

```python
'game-slug': '/game-slug',
```

Without this, the Games page Play button links to the landing page instead of the score sheet.

Note: slug and URL path can differ (e.g., `'uno-classic': '/uno'`).

### 7. Dashboard: `app/templates/dashboard.html`

Add **6 insertion points** for the new slug:

**Card view icon** (after last `{% elif game.slug == ... %}` before `{% else %}`):
```html
{% elif game.slug == 'game-slug' %}<i class="fas fa-icon me-1" style="color:#HEX;"></i>
```

**Card view Continue link:**
```html
{% elif game.slug == 'game-slug' %}
  <a href="{{ url_for('main.game_slug_fn') }}?game_id={{ game.id }}" class="btn btn-primary w-100"><i class="fas fa-play me-1"></i>Continue</a>
```

**Table view icon** (same pattern as card view icon)

**Table view Continue link** (btn-sm variant):
```html
{% elif game.slug == 'game-slug' %}
  <a href="{{ url_for('main.game_slug_fn') }}?game_id={{ game.id }}" class="btn btn-primary btn-sm"><i class="fas fa-play"></i></a>
```

### 8. Verify and Restart

```bash
# Verify Jinja2 template parses
cd /home/joe/DiFedeAppV2 && source venv/bin/activate
python3 -c "from jinja2 import Environment, FileSystemLoader; Environment(loader=FileSystemLoader('app/templates')).get_template('game_slug.html'); print('OK')"

# Kill stale processes and restart
lsof -i :5002 -t | xargs -r kill -9
sudo systemctl restart difedeappv2.service
# OR for dev: python3 run.py

# Verify
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:5002/game-slug -L
lsof -i :5002
```

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
6. Set `image_url` in games table

### Existing Game Images

| Game | Filename | Key Visual Elements |
|------|----------|-------------------|
| Five Crowns | five_crowns.png | Crown, playing cards |
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
| `/api/players` | GET | Get players (family + crew) | (none) |
| `/api/players` | POST | Create a new player | `{first_name, last_name, display_name}` |
| `/api/game-details/<slug>` | GET | Get rules for Rules modal | (none) |

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
    <!-- New Game: paused list, completed list, player count, dropdowns, target score, Start -->
  {% else %}
    <!-- Active Game: header, score table, action buttons, point reference panel -->
  {% endif %}
</div>
<!-- Modals OUTSIDE container -->
{% endblock %}

{% block styles %} ... game theme CSS, mobile @media ... {% endblock %}
{% block scripts %} ... jQuery, SocketIO, score save, totals ... {% endblock %}
```

Score cells: `class="score-cell" data-player="X" data-round="Y"`
Total cells: `class="total-cell" data-player="X"`

---

## SocketIO Events

| Event | Direction | When |
|-------|-----------|------|
| `join_game` | Emit | On connect if active game: `{game_id: activeGameId}` |
| `leave_game` | Emit | On page unload |
| `score_update` | Listen | Another user updated a score |
| `game_completed` | Listen | Another user completed the game |
| `game_paused` | Listen | Another user paused the game |
| `game_resumed` | Listen | Another user resumed the game |

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

| ID | Name | Slug | Route | Scoring | Target | Type |
|----|------|------|-------|---------|--------|------|
| 1 | Five Crowns | five-crowns | /five-crowns | low_wins | NULL | Standalone |
| 2 | UNO | uno | -- | -- | -- | Variant group |
| 3 | UNO Classic | uno-classic | /uno | high_wins | 500 | Variant of 2 |
| 4 | UNO Flip | uno-flip | /uno-flip | high_wins | 500 | Variant of 2 |
| 5 | Dutch Blitz | dutch-blitz | /dutch-blitz | high_wins | 75 | Standalone |
| 6 | Trouble | trouble | /trouble | high_wins | NULL | Standalone (Pattern B) |
| 7 | Basic / Other | basic-other | /basic-other | configurable | NULL | Standalone (Pattern C) |
| 8 | Kings in the Corner | kings-corner | /kings-corner | low_wins | NULL | Standalone (A + B) |
| 9 | Gin Rummy | gin-rummy | /gin-rummy | high_wins | 100 | Standalone |
| 10 | Sevens | sevens | /sevens | low_wins | 100 | Standalone |

---

## The `game_page()` Function

Central handler in `routes.py` (~line 107). Takes slug and games.id:

1. Loads game definition from `games` table
2. Resolves active session via `?game_id=` or most recent incomplete; auto-unpauses
3. Loads paused games, last 5 completed (with `game_sessions_numbered` for game_number)
4. If active: loads players + scores from `game_scores` (fallback `five_crowns_scores` for Five Crowns only)
5. Loads selectable players via `get_family_players(family_id, include_crew=True)`
6. Renders `slug.replace('-', '_') + '.html'`

**Template variables:**
- `game_def`, `active_game`, `paused_games`, `completed_games`
- `game_players`, `scores`, `players`, `game_family_id`, `user_family_id`

---

## Known Pitfalls

| Problem | Fix |
|---------|-----|
| Internal Server Error on game page | Check Jinja `function(){{% if` -> add space |
| Invisible text on forms/score sheets | Add `color: #212529` on light backgrounds |
| Play button goes to landing page | Add slug to `SLUG_TO_URL` |
| Dashboard Continue goes to /games | Add slug cases in dashboard.html (6 places) |
| Stale code after changes | Kill all port 5002 processes before restart |
| Game # shows wrong order | Use `game_number` from view, sort numerically in DataTables |
| StrReplace fails on dashboard.html | File may have CRLF line endings; run `sed -i 's/\r$//' dashboard.html` |
