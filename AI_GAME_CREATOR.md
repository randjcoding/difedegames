# AI-Powered Game Creator System - Technical Specification

## Overview

This document describes a system that allows DiFede Games users (or admins) to add new game types to the platform through a guided, AI-assisted process. The user answers a structured questionnaire about the game, submits it to the Anthropic Claude API, and the system generates all necessary code, templates, database entries, and configuration to make the new game playable -- with full rollback capability if anything goes wrong.

---

## Why This Works

DiFede Games already has a clean, repeatable architecture for adding games:

1. A row in the `games` table defines the game metadata
2. A row in `game_details` stores rules, scoring info, strategy tips
3. A Jinja2 template renders the scoresheet UI
4. A route alias in `routes.py` connects the URL to `game_page()`
5. An entry in `SLUG_TO_URL` maps the slug to the route
6. Scores go into the unified `game_scores` table (player_id, round_number, score, metadata JSONB)
7. The `active_games` and `active_game_players` tables track game sessions

Because this pattern is consistent, an AI can generate all the pieces from a structured spec.

---

## Architecture

```
User (Browser)
     |
     v
[Game Creator Wizard UI]  <-- Multi-step form in the browser
     |
     v
[Flask API: /api/admin/create-game]
     |
     v
[Validation Layer] -- checks for missing info, asks follow-ups
     |
     v
[Anthropic Claude API] -- generates template, CSS, JS, SQL
     |
     v
[Code Assembly & Validation] -- assembles pieces, runs syntax checks
     |
     v
[Rollback Snapshot] -- DB + file backup before any writes
     |
     v
[Deployment] -- writes template, updates routes, inserts DB rows
     |
     v
[Verification] -- test render, basic smoke test
```

---

## Phase 1: The Questionnaire

The wizard collects everything the AI needs. Each question maps to a concrete output.

### Required Information

| # | Question | Maps To | Example |
|---|----------|---------|---------|
| 1 | Game name | `games.name`, slug generation | "Phase 10" |
| 2 | Short description (1-2 sentences) | `games.description` | "A rummy-type card game where players race to complete 10 phases" |
| 3 | Minimum players | `games.min_players` | 2 |
| 4 | Maximum players | `games.max_players` | 6 |
| 5 | Does the game have rounds/hands? | `games.has_rounds` | Yes |
| 6 | If rounds: are they numbered (1, 2, 3...) or named (3s, 4s, 5s...)? | Template round labels | Named: "Phase 1" through "Phase 10" |
| 7 | List all round names/labels (if named) | Template round rendering | ["Phase 1", "Phase 2", ..., "Phase 10"] |
| 8 | Scoring direction: does high or low score win? | `games.scoring_direction` | "low_wins" |
| 9 | Is there a target score to end the game? | `games.default_target_score` | null (Phase 10 ends after 10 phases) |
| 10 | Can the user choose scoring direction at game start? | Template toggle visibility | No (always low wins) |
| 11 | Can the user set a custom target score? | Template toggle visibility | No |
| 12 | What gets scored each round? Just a number, or multiple fields? | Score input type | Single number per player per round |
| 13 | Are there special scoring rules? (e.g., bonus points, penalties) | `game_details.scoring_system` | "Players who don't complete their phase get all points from remaining cards added to their score" |
| 14 | Is there a dealer that rotates? | Template dealer tracking | Yes |
| 15 | Primary color scheme (hex or description) | Template CSS variables | "#E53935" (red) or "forest green and gold" |
| 16 | Secondary/accent color | Template CSS | "#FDD835" (yellow) |
| 17 | Game image (upload) or use default? | `games.image_url` | Upload or default placeholder |
| 18 | Full rules text | `game_details.rules` | (Paste or type full rules) |
| 19 | Strategy tips (optional) | `game_details.tips_and_strategies` | "Focus on completing phases over getting low scores" |
| 20 | Equipment needed | `game_details.equipment_needed` | "Phase 10 card deck (108 cards)" |
| 21 | Estimated game duration | `game_details.estimated_duration_minutes` | 45 |
| 22 | Age recommendation | `game_details.age_recommendation` | "7+" |
| 23 | Difficulty level | `game_details.difficulty_level` | "Easy" |
| 24 | Any point value reference card needed? (like UNO point values) | Template pinnable panel | Yes: "1-9 = 5pts, 10-12 = 10pts, Skip = 15pts, Wild = 25pts" |

### Follow-up Logic

The Anthropic API reviews the answers and can ask follow-up questions:
- "You said rounds are named but didn't provide labels. What are the round names?"
- "You said there are special scoring rules but the description is vague. Can you give a concrete example?"
- "Your min_players is 1 but most card games need 2+. Is this a solo game variant?"

---

## Phase 2: What the AI Generates

Given complete answers, Claude generates these artifacts:

### 2a. SQL Statements

```sql
-- Insert into games table
INSERT INTO games (name, slug, min_players, max_players, scoring_direction,
  default_target_score, has_rounds, image_url, description, parent_game_id, is_variant_group)
VALUES ('Phase 10', 'phase-10', 2, 6, 'low_wins', NULL, TRUE,
  '/static/images/phase_10.png', 'A rummy-type card game...', NULL, FALSE);

-- Insert into game_details
INSERT INTO game_details (game_id, rules, scoring_system, equipment_needed,
  estimated_duration_minutes, difficulty_level, age_recommendation, ...)
VALUES ((SELECT id FROM games WHERE slug = 'phase-10'), '...', '...', ...);
```

### 2b. Jinja2 Template

A complete `.html` template file following the established pattern:
- Extends `base.html`
- Has `{% block content %}`, `{% block styles %}`, `{% block scripts %}`
- New game section with player selection (grouped by family/crew)
- Paused games list
- Scoresheet table with:
  - Dynamic column widths based on player count
  - Sticky round/header columns
  - Mobile-responsive scrolling
  - Score input cells that save via `/api/scores`
- Game actions (pause, complete, dealer toggle)
- Round-by-round mobile view (if applicable)
- Fullscreen editable modal (placed OUTSIDE any container with z-index)
- Point reference panel (if applicable)
- Game settings (scoring direction, target score -- if configurable)
- Uses theme CSS variables (`--df-*`) exclusively, no hardcoded colors except game-specific accent

### 2c. Route Registration

Python code to add to `routes.py`:

```python
@main.route('/phase-10')
@login_required
def phase_10():
    return game_page('phase-10', <new_game_id>)
```

And the `SLUG_TO_URL` update:

```python
SLUG_TO_URL['phase-10'] = '/phase-10'
```

### 2d. CSS Custom Properties

Game-specific color overrides scoped to the game container:

```css
.phase-10-container {
    --game-primary: #E53935;
    --game-secondary: #FDD835;
    --game-header-bg: linear-gradient(135deg, #E53935, #C62828);
}
```

---

## Phase 3: Rollback System

Before ANY writes happen:

### Database Snapshot
```python
import subprocess
timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
subprocess.run([
    'pg_dump', '-U', 'difedeapp', '-d', 'difedeappv2',
    '-f', f'/home/joe/DiFedeAppV2/backups/pre_game_create_{timestamp}.sql'
])
```

### File Snapshot
```python
import shutil
template_backup_dir = f'/home/joe/DiFedeAppV2/backups/templates_{timestamp}'
shutil.copytree('/home/joe/DiFedeAppV2/app/templates', template_backup_dir)

routes_backup = f'/home/joe/DiFedeAppV2/backups/routes_{timestamp}.py'
shutil.copy2('/home/joe/DiFedeAppV2/app/routes.py', routes_backup)
```

### Rollback Function
```python
def rollback_game_creation(timestamp):
    """Restore DB and files to pre-creation state"""
    # 1. Drop the new game entries
    # 2. Restore routes.py from backup
    # 3. Remove the new template file
    # 4. If anything fails, restore full DB from pg_dump
```

### Rollback Triggers
- Template syntax error (Jinja2 parse failure)
- Route registration conflict
- DB constraint violation
- Manual admin "Undo" button (available for 24 hours after creation)

---

## Phase 4: Anthropic API Integration

### What Already Exists

You already have a working Anthropic integration from the BCBA Test Prep project. Here's what we're starting with:

**config_ai.py** (from BCBA project):
- API key: stored in `config_ai.py` (gitignored; supply your own Anthropic key)
- Model: `claude-sonnet-4-5` (Claude 4.5 Sonnet, dynamic alias -- always latest Sonnet)
- Max tokens: 2,000 (needs increase for game generation)
- Temperature: 0.7 (needs decrease for code generation)
- Has a system prompt (BCBA-specific, will be replaced for game creation)

**ai_service.py** (from BCBA project):
- `AIService` class with `anthropic.Anthropic(api_key=...)` client
- `test_connection()` method -- reusable as-is
- JSON response parsing with regex fallback (`re.search(r'\{[\s\S]*\}', ...)`) -- reusable as-is
- Cost calculation logic ($3/M input, $15/M output for Sonnet) -- reusable as-is
- Token usage tracking -- reusable as-is
- Error handling with typed exceptions -- reusable as-is

### What Needs to Change for Game Creator

The existing `AIService` class is a solid foundation. We adapt it, not rewrite it.

**New config: `app/config_ai.py`** (inside DiFedeAppV2):

```python
"""
AI Configuration for DiFede Games - Game Creator
API key and model settings for Anthropic Claude integration.
Local machine only -- API key is not exposed to any external service.
"""

ANTHROPIC_API_KEY = "sk-ant-your-key-here"  # do not commit real keys

# Model: Claude 4.5 Sonnet (dynamic alias, always resolves to latest Sonnet)
# This is the same model used in the BCBA project -- proven reliable.
AI_MODEL = "claude-sonnet-4-5"

# Token limits -- game template generation needs significantly more output
# than BCBA explanations (2,000 is not enough for a full Jinja2 template + CSS + JS).
# A complete game template runs 800-1,500 lines. With SQL + route code, 8,192 is safe.
AI_MAX_TOKENS = 8192

# Temperature -- lower than BCBA (0.7) because we want deterministic, correct code,
# not creative prose. 0.2 gives consistent, structured output.
AI_TEMPERATURE = 0.2

PROMPT_VERSION = "1.0"
```

**New service: `app/ai_game_service.py`** (adapted from your existing `AIService` pattern):

```python
"""
AI Game Creator Service for DiFede Games
Adapted from the BCBA Test Prep AIService pattern.
Handles game specification -> code generation via Anthropic Claude.
"""

import anthropic
import json
import re
from datetime import datetime
from app.config_ai import (
    ANTHROPIC_API_KEY, AI_MODEL, AI_MAX_TOKENS,
    AI_TEMPERATURE, PROMPT_VERSION
)

GAME_CREATOR_SYSTEM_PROMPT = """You are a code generator for the DiFede Games application.
This is a Flask + PostgreSQL + Bootstrap 5 app that tracks scores for card and board games
across families. It uses Jinja2 templates, Flask-SocketIO for real-time updates, and a
unified game_scores table for all score persistence.

You generate ONLY the following artifacts from a game specification:
1. SQL INSERT statements for the `games` and `game_details` tables
2. A complete Jinja2 HTML template following the established Five Crowns/UNO pattern
3. A Python route snippet to add to routes.py
4. A SLUG_TO_URL dictionary entry

CRITICAL RULES (violating any of these produces unusable output):
- Use CSS variables (--df-*) for ALL theme colors. Never hardcode theme colors.
- Game-specific accent colors are allowed (e.g., UNO red #ED1C24, Five Crowns purple #5d2d91).
- The template MUST extend base.html.
- Score cells MUST save via POST to /api/scores with {active_game_id, player_id, round_number, score}.
- The fullscreen modal MUST be placed OUTSIDE any game container div (z-index stacking context issue).
- Player select dropdowns MUST use <optgroup> labels for "Your Family" and "Crew" groups.
- Mobile layout MUST use sticky first column, horizontal scrolling, and dynamic column widths.
- NEVER use emojis in code, comments, or UI text.
- NEVER use DataTables.js. Use plain HTML tables with custom CSS.
- All Bootstrap 5 modals MUST be placed outside any container with z-index to avoid backdrop trapping.
- Include SocketIO connection: var socket = io({transports:['websocket','polling']});
- Include socket.emit('join_game', {game_id: gameId}) on page load.
- Game container class follows pattern: .SLUG-container (e.g., .phase-10-container)
- Use .btn-game-toggle class for scoring direction and target score toggle buttons.

OUTPUT FORMAT:
Return a JSON object with these exact keys:
{
  "sql": "-- Full SQL INSERT statements here",
  "template_html": "<!-- Full Jinja2 template here -->",
  "route_python": "# Python route function here",
  "slug_map_entry": "SLUG_TO_URL['slug'] = '/slug'",
  "validation_notes": []
}

If any required information is missing or ambiguous, populate "validation_notes" with
an array of specific questions. Do NOT generate code if information is incomplete."""


class GameCreatorAI:
    """
    Adapted from BCBA project's AIService.
    Same client pattern, same JSON parsing, same cost tracking.
    Different system prompt, higher token limit, lower temperature.
    """

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        self.model = AI_MODEL
        self.max_tokens = AI_MAX_TOKENS
        self.temperature = AI_TEMPERATURE
        self.system_prompt = GAME_CREATOR_SYSTEM_PROMPT

    def test_connection(self):
        """Reused from BCBA AIService -- confirms API key is valid"""
        try:
            message = self.client.messages.create(
                model=self.model,
                max_tokens=100,
                messages=[
                    {"role": "user", "content": "Say 'API connection successful!' and nothing else."}
                ]
            )
            return {
                'success': True,
                'message': message.content[0].text,
                'tokens': {
                    'input': message.usage.input_tokens,
                    'output': message.usage.output_tokens
                }
            }
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def generate_game(self, game_spec: dict) -> dict:
        """
        Send structured game spec to Claude, get back code artifacts.
        Uses the same JSON extraction pattern from BCBA's generate_explanation().
        """
        try:
            message = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                system=self.system_prompt,
                messages=[{
                    "role": "user",
                    "content": f"Generate a new game for DiFede Games:\n\n{json.dumps(game_spec, indent=2)}"
                }]
            )

            response_text = message.content[0].text

            # JSON extraction -- same pattern from BCBA ai_service.py
            try:
                json_match = re.search(r'\{[\s\S]*\}', response_text)
                if json_match:
                    result = json.loads(json_match.group())
                else:
                    result = json.loads(response_text)
            except json.JSONDecodeError as e:
                result = {
                    'raw_response': response_text,
                    'validation_notes': [f'Failed to parse AI response as JSON: {str(e)}']
                }

            # Cost calculation -- same formula from BCBA ai_service.py
            input_cost = (message.usage.input_tokens / 1_000_000) * 3.0
            output_cost = (message.usage.output_tokens / 1_000_000) * 15.0

            return {
                'success': True,
                'artifacts': result,
                'metadata': {
                    'model': self.model,
                    'prompt_version': PROMPT_VERSION,
                    'generated_at': datetime.now().isoformat(),
                    'tokens': {
                        'input': message.usage.input_tokens,
                        'output': message.usage.output_tokens,
                        'total': message.usage.input_tokens + message.usage.output_tokens
                    },
                    'cost': {
                        'input': round(input_cost, 6),
                        'output': round(output_cost, 6),
                        'total': round(input_cost + output_cost, 6)
                    }
                }
            }

        except Exception as e:
            return {
                'success': False,
                'error': str(e),
                'error_type': type(e).__name__
            }
```

### Key Differences from BCBA Integration

| Setting | BCBA Project | Game Creator | Why |
|---------|-------------|-------------|-----|
| Max tokens | 2,000 | 8,192 | Full template + SQL + route is 800-1,500 lines |
| Temperature | 0.7 | 0.2 | Code gen needs consistency, not creativity |
| System prompt | Dr. Elena persona | Code generator rules | Completely different purpose |
| Output format | Structured JSON (explanation) | Structured JSON (sql, template, route) | Same pattern, different keys |
| Error handling | Same | Same | Identical try/except + JSON fallback |
| Cost tracking | Same | Same | Identical formula |

### Flask API Endpoint

```python
@main.route('/api/admin/create-game', methods=['POST'])
@login_required
def create_game_ai():
    user = get_current_user()
    if user.get('role') != 'super_admin':
        return jsonify({'error': 'Unauthorized'}), 403

    game_spec = request.json
    ai = GameCreatorAI()

    # Step 1: Send to Anthropic (uses your existing API key)
    result = ai.generate_game(game_spec)

    if not result['success']:
        return jsonify({'error': result['error']}), 500

    artifacts = result['artifacts']

    # Step 2: Check for gaps (same pattern as BCBA's validation)
    if artifacts.get('validation_notes'):
        return jsonify({
            'status': 'needs_info',
            'questions': artifacts['validation_notes'],
            'cost': result['metadata']['cost']
        }), 200

    # Step 3: Create rollback point
    timestamp = create_backup()

    try:
        # Step 4: Execute SQL
        execute_sql(artifacts['sql'])

        # Step 5: Write template file
        write_template(artifacts['template_html'], game_spec['slug'])

        # Step 6: Register route (append to routes.py)
        register_route(artifacts['route_python'], artifacts['slug_map_entry'])

        # Step 7: Verify template renders
        verify_template(game_spec['slug'])

        return jsonify({
            'status': 'success',
            'slug': game_spec['slug'],
            'rollback_id': timestamp,
            'cost': result['metadata']['cost'],
            'tokens': result['metadata']['tokens'],
            'message': f"Game '{game_spec['name']}' created. Restart server to activate."
        })

    except Exception as e:
        rollback_game_creation(timestamp)
        return jsonify({
            'error': str(e),
            'rolled_back': True,
            'rollback_id': timestamp
        }), 500
```

---

## Phase 5: The Wizard UI

A multi-step form in the admin console:

### Step 1: Basic Info
- Game name (text)
- Short description (textarea)
- Player count range (min/max number inputs)
- Game type: "Card Game", "Board Game", "Dice Game", "Other"

### Step 2: Scoring
- Has rounds? (toggle)
- Round type: Numbered or Named (radio)
- Round labels (dynamic list input, if named)
- Scoring direction: High wins / Low wins / User chooses (radio)
- Target score: None / Fixed / User chooses (radio + number input)
- Special scoring rules (textarea)
- Point value reference needed? (toggle + textarea for values)

### Step 3: Look and Feel
- Primary color (color picker)
- Secondary color (color picker)
- Game image (file upload with drag-drop, or "Use Default")
- Dealer tracking? (toggle)

### Step 4: Rules and Details
- Full rules (rich textarea)
- Equipment needed (text)
- Strategy tips (textarea)
- Duration, age recommendation, difficulty (quick inputs)

### Step 5: Review and Submit
- Summary of all inputs
- "Generate Game" button
- Progress indicator during AI generation
- Display any follow-up questions from the AI
- Preview of the generated template (iframe or rendered HTML)
- "Deploy" or "Cancel" buttons
- Confirmation modal with rollback info

---

## Cost Considerations

### Anthropic API Pricing (Claude 4.5 Sonnet -- your current model)
- Input: $3 per 1M tokens
- Output: $15 per 1M tokens
- (Same rates used in your BCBA project's cost calculation)

### Estimated Cost Per Game Creation
- System prompt + game spec: ~3,000-4,000 input tokens
- Full template + SQL + route output: ~6,000-10,000 output tokens
- **Single generation: $0.10 - $0.18**
- Follow-up round (gap-filling): ~$0.04-$0.08 each
- Typical total including 1 follow-up: **$0.15 - $0.25**
- For context: generating 20 games would cost roughly $3-$5

### Rate Limiting
- Limit to super_admin only (currently just joe_71@yahoo.com)
- Max 5 game creations per day
- Store API call logs with timestamps and token usage (same metadata pattern from BCBA)

### If This Goes Commercial
- Game creation becomes a premium/paid feature
- Each user org gets N free game creations per month
- Additional creations charged at cost + margin
- Pre-built game library (Phase 10, Skipbo, Yahtzee, etc.) ships free -- no API call needed
- Only custom/novel games hit the API

---

## Implementation Roadmap

### Already Done (from BCBA project)
- [x] `anthropic` Python package installed and working
- [x] API key confirmed active (stored locally in gitignored `config_ai.py`)
- [x] `claude-sonnet-4-5` model tested and proven reliable
- [x] `AIService` class pattern with connection test, JSON parsing, cost tracking, error handling
- [x] JSON extraction from AI responses (handles Claude's tendency to wrap JSON in prose)
- [x] Cost calculation formula validated ($3/M input, $15/M output)

### Sprint 1: Foundation (Estimated: 3-4 hours)
- [ ] Copy and adapt `config_ai.py` into `app/config_ai.py` (change tokens to 8192, temp to 0.2)
- [ ] Create `app/ai_game_service.py` with `GameCreatorAI` class (adapt from existing `AIService`)
- [ ] Write the GAME_CREATOR_SYSTEM_PROMPT (the big one -- include complete template examples)
- [ ] Create the `/api/admin/create-game` endpoint
- [ ] Build the rollback/backup system (pg_dump + file copy)
- [ ] Test with a manual JSON payload via curl (e.g., Phase 10 spec)
- [ ] Verify generated template renders without syntax errors

### Sprint 2: Wizard UI (Estimated: 3-4 hours)
- [ ] Create `admin_game_creator.html` template with multi-step wizard
- [ ] Add color picker, file upload, dynamic round label inputs
- [ ] Wire wizard form to the `/api/admin/create-game` endpoint
- [ ] Handle follow-up questions flow (AI asks, user answers, re-submit)
- [ ] Add generated template preview (iframe sandbox)
- [ ] Add cost display after generation (tokens used, dollar amount)

### Sprint 3: Polish and Safety (Estimated: 2-3 hours)
- [ ] Jinja2 template syntax validation before writing to disk
- [ ] Route conflict detection (check existing SLUG_TO_URL before registering)
- [ ] "Undo" button in admin for 24-hour rollback window
- [ ] API usage logging table (`ai_usage_log`: timestamp, tokens, cost, game_slug, user_id)
- [ ] Admin notification when a new game is created
- [ ] Server restart integration (kill + restart via subprocess, or Jinja2 hot-reload)

### Sprint 4: Pre-built Library (Estimated: 2-3 hours)
- [ ] Create a library of pre-generated game specs (JSON files in `/app/game_library/`)
- [ ] "Quick Add" option: select from library, customize colors, deploy without API call
- [ ] Initial library: Phase 10, Skipbo, Spades, Hearts, Rummy 500, Yahtzee, Canasta
- [ ] Each library entry includes a pre-validated template (no API cost for common games)

---

## Database Tables Involved

### `games` - Game type definition
| Column | Type | Purpose |
|--------|------|---------|
| id | SERIAL PK | Auto-generated |
| name | TEXT | Display name |
| slug | VARCHAR | URL slug (auto-generated from name) |
| min_players | INT | Minimum player count |
| max_players | INT | Maximum player count |
| scoring_direction | VARCHAR | 'high_wins' or 'low_wins' |
| default_target_score | INT | NULL if no target |
| has_rounds | BOOL | Whether game has discrete rounds |
| image_url | TEXT | Path to game image |
| description | TEXT | Short description |
| parent_game_id | INT FK | For variants (e.g., UNO Classic -> UNO) |
| is_variant_group | BOOL | TRUE for parent groups |

### `game_details` - Extended game info
| Column | Type | Purpose |
|--------|------|---------|
| id | SERIAL PK | Auto-generated |
| game_id | INT FK | Links to games.id |
| rules | TEXT | Full rules HTML |
| scoring_system | TEXT | Scoring explanation |
| equipment_needed | TEXT | What you need to play |
| estimated_duration_minutes | INT | Typical game length |
| difficulty_level | VARCHAR | Easy/Medium/Hard |
| age_recommendation | VARCHAR | "7+", "12+", etc. |
| tips_and_strategies | TEXT | Strategy tips |

### `game_scores` - Unified score storage
| Column | Type | Purpose |
|--------|------|---------|
| id | SERIAL PK | Auto-generated |
| active_game_id | INT FK | Links to active_games.id |
| player_id | INT FK | Links to players.id |
| round_number | INT | Round sequence |
| score | INT | The score value |
| metadata | JSONB | Flexible extra data (side, bonus, etc.) |
| created_at | TIMESTAMP | Auto-set |

### `active_games` - Game sessions
| Column | Type | Purpose |
|--------|------|---------|
| id | SERIAL PK | Auto-generated |
| game_id | INT FK | Links to games.id |
| user_id | INT FK | Who created the session |
| family_id | INT FK | Family context |
| start_time | TIMESTAMP | When started |
| completion_time | TIMESTAMP | When finished |
| is_complete | BOOL | Game finished? |
| is_paused | BOOL | Game paused? |
| scoring_direction | VARCHAR | Override per-session |
| target_score | INT | Override per-session |

---

## Template Pattern Reference

Every game template follows this structure:

```
{% extends "base.html" %}
{% block title %}GAME NAME{% endblock %}
{% block content %}
<div class="SLUG-container">            <-- Outer themed container
  {% if not active_game %}
    <div class="new-game-section">      <-- New game form
      - Paused games list
      - Player count selector
      - Player dropdowns (family/crew optgroups)
      - Game settings (if applicable)
      - Start button
    </div>
  {% else %}
    <div class="game-board">            <-- Active game view
      - Game header with info button
      - Score table (responsive)
      - Running totals
      - Game actions (pause, complete, dealer, view toggles)
    </div>
  {% endif %}
</div>

<!-- Modals OUTSIDE the container -->
<div id="fullscreenTableModal">...</div>
<div id="addPlayerModal">...</div>
<div id="rulesModal">...</div>

{% endblock %}

{% block styles %}
<style>
  .SLUG-container { ... game-specific theming ... }
  /* Mobile overrides */
  @media (max-width: 768px) { ... }
</style>
{% endblock %}

{% block scripts %}
<script>
  // SocketIO connection
  // Score update function (saves to /api/scores)
  // Table rendering
  // Mobile view toggles
  // Dealer tracking
  // Fullscreen modal builder
</script>
{% endblock %}
```

---

## Security Considerations

- Super admin only (role check on API endpoint -- currently just joe_71@yahoo.com)
- API key lives in `app/config_ai.py` on the local server only. This machine is not
  publicly exposed (app runs behind firewall on port 5002). If this ever goes to a
  public server, move the key to an environment variable or `.env` file excluded from git.
- AI-generated SQL is parameterized and reviewed before execution
- Template code is sandboxed (Jinja2 autoescaping is on by default)
- Generated Python route code is validated against a whitelist pattern (must match
  `@main.route('/<slug>') + def slug_fn(): return game_page(...)`)
- No arbitrary code execution from AI output -- only SQL, HTML templates, and route stubs
- pg_dump backups created before every game creation attempt
- If this goes commercial: API key moves to environment variable, rate limiting per-org,
  API calls logged to an audit table

---

## What's Already In Place vs. What Needs Building

### Have (from BCBA project + existing DiFede architecture):
- Working Anthropic API key and `anthropic` Python SDK
- Proven `AIService` class pattern (connection test, JSON parsing, cost tracking)
- `claude-sonnet-4-5` model confirmed working
- Clean, repeatable game architecture (games table -> template -> route -> SLUG_TO_URL)
- Unified `game_scores` table that works for any game type
- Generic `game_page()` handler that loads any game by slug + game_id

### Need to Build:
- `app/config_ai.py` -- copy from BCBA, adjust tokens and temperature
- `app/ai_game_service.py` -- adapt `AIService` into `GameCreatorAI`
- The SYSTEM_PROMPT -- the single most important piece. Must include a complete
  example template (e.g., a simplified Five Crowns) so the AI has a concrete reference.
- `/api/admin/create-game` endpoint
- Rollback system (pg_dump + file backup + undo function)
- Wizard UI (multi-step form in admin console)
- Template validation (Jinja2 parse check before writing to disk)

---

## Summary

This system turns adding a new game from a 2-4 hour manual coding task into a 10-minute
guided wizard. You already have 60% of the infrastructure from the BCBA project -- the
Anthropic SDK, the API key, the service class pattern, the JSON parsing, the cost tracking.
The game architecture is clean and repeatable enough that an AI can generate all the pieces
from a structured spec. The remaining work is writing the system prompt (with complete
template examples), building the wizard UI, and adding the rollback safety net. Total
estimated effort: 10-14 hours across 4 sprints.
