-- DiFedeAppV2 Database Migration: Multi-game scalable schema
-- Run against: difedeappv2
-- Date: 2026-03-03

BEGIN;

-- ============================================================
-- 1. Create families table
-- ============================================================
CREATE TABLE IF NOT EXISTS families (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    created_by_user_id INTEGER,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Populate from existing users.family_name
INSERT INTO families (name, created_by_user_id)
SELECT DISTINCT u.family_name, MIN(u.id)
FROM users u
WHERE u.family_name IS NOT NULL AND u.family_name != ''
GROUP BY u.family_name
ON CONFLICT (name) DO NOTHING;

-- Add family_id FK to users
ALTER TABLE users ADD COLUMN IF NOT EXISTS family_id INTEGER;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_approved BOOLEAN DEFAULT FALSE;

-- Backfill family_id for existing users
UPDATE users u SET family_id = f.id
FROM families f WHERE f.name = u.family_name AND u.family_id IS NULL;

-- Auto-approve existing verified users
UPDATE users SET is_approved = TRUE WHERE is_verified = TRUE;
-- Auto-approve the super_admin
UPDATE users SET is_approved = TRUE WHERE role = 'super_admin';

-- Add FK constraint
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'users_family_id_fkey') THEN
        ALTER TABLE users ADD CONSTRAINT users_family_id_fkey
            FOREIGN KEY (family_id) REFERENCES families(id) ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_users_family_id ON users(family_id);

-- ============================================================
-- 2. Add family_id to players
-- ============================================================
ALTER TABLE players ADD COLUMN IF NOT EXISTS family_id INTEGER;

-- Backfill: players inherit family from their owner user
UPDATE players p SET family_id = u.family_id
FROM users u WHERE u.id = p.user_id AND p.family_id IS NULL;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'players_family_id_fkey') THEN
        ALTER TABLE players ADD CONSTRAINT players_family_id_fkey
            FOREIGN KEY (family_id) REFERENCES families(id) ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_players_family_id ON players(family_id);

-- ============================================================
-- 3. Extend games table for multi-game support
-- ============================================================
ALTER TABLE games ADD COLUMN IF NOT EXISTS scoring_direction VARCHAR(20) DEFAULT 'low_wins';
ALTER TABLE games ADD COLUMN IF NOT EXISTS default_target_score INTEGER;
ALTER TABLE games ADD COLUMN IF NOT EXISTS has_rounds BOOLEAN DEFAULT TRUE;
ALTER TABLE games ADD COLUMN IF NOT EXISTS image_url TEXT;
ALTER TABLE games ADD COLUMN IF NOT EXISTS parent_game_id INTEGER;
ALTER TABLE games ADD COLUMN IF NOT EXISTS is_variant_group BOOLEAN DEFAULT FALSE;
ALTER TABLE games ADD COLUMN IF NOT EXISTS slug VARCHAR(100);
ALTER TABLE games ADD COLUMN IF NOT EXISTS description TEXT;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'games_parent_game_id_fkey') THEN
        ALTER TABLE games ADD CONSTRAINT games_parent_game_id_fkey
            FOREIGN KEY (parent_game_id) REFERENCES games(id) ON DELETE SET NULL;
    END IF;
END $$;

-- Update existing Five Crowns
UPDATE games SET
    scoring_direction = 'low_wins',
    has_rounds = TRUE,
    slug = 'five-crowns',
    description = 'A fun rummy-style card game with rotating wild cards'
WHERE id = 1;

-- ============================================================
-- 4. Insert UNO games
-- ============================================================

-- UNO parent group
INSERT INTO games (name, min_players, max_players, scoring_direction, default_target_score, has_rounds, is_variant_group, slug, description)
SELECT 'UNO', 2, 10, 'high_wins', 500, TRUE, TRUE, 'uno', 'The classic card game of matching colors and numbers'
WHERE NOT EXISTS (SELECT 1 FROM games WHERE slug = 'uno');

-- UNO Classic
INSERT INTO games (name, min_players, max_players, scoring_direction, default_target_score, has_rounds, parent_game_id, slug, description)
SELECT 'UNO Classic', 2, 10, 'high_wins', 500, TRUE,
    (SELECT id FROM games WHERE slug = 'uno'), 'uno-classic',
    'The original UNO card game - match colors, numbers, and action cards'
WHERE NOT EXISTS (SELECT 1 FROM games WHERE slug = 'uno-classic');

-- UNO Flip
INSERT INTO games (name, min_players, max_players, scoring_direction, default_target_score, has_rounds, parent_game_id, slug, description)
SELECT 'UNO Flip', 2, 10, 'high_wins', 500, TRUE,
    (SELECT id FROM games WHERE slug = 'uno'), 'uno-flip',
    'UNO with a twist - double-sided cards with Light and Dark sides'
WHERE NOT EXISTS (SELECT 1 FROM games WHERE slug = 'uno-flip');

-- ============================================================
-- 5. Extend active_games for multi-game flexibility
-- ============================================================
ALTER TABLE active_games ADD COLUMN IF NOT EXISTS scoring_direction VARCHAR(20);
ALTER TABLE active_games ADD COLUMN IF NOT EXISTS target_score INTEGER;
ALTER TABLE active_games ADD COLUMN IF NOT EXISTS is_locked BOOLEAN DEFAULT FALSE;
ALTER TABLE active_games ADD COLUMN IF NOT EXISTS family_id INTEGER;

-- Backfill family_id for existing games from the user who created them
UPDATE active_games ag SET family_id = u.family_id
FROM users u WHERE u.id = ag.user_id AND ag.family_id IS NULL;

-- Backfill scoring_direction for existing Five Crowns games
UPDATE active_games SET scoring_direction = 'low_wins'
WHERE game_id = 1 AND scoring_direction IS NULL;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'active_games_family_id_fkey') THEN
        ALTER TABLE active_games ADD CONSTRAINT active_games_family_id_fkey
            FOREIGN KEY (family_id) REFERENCES families(id) ON DELETE SET NULL;
    END IF;
END $$;

-- ============================================================
-- 6. Extend active_game_players
-- ============================================================
ALTER TABLE active_game_players ADD COLUMN IF NOT EXISTS can_edit_scores BOOLEAN DEFAULT TRUE;
ALTER TABLE active_game_players ADD COLUMN IF NOT EXISTS family_id INTEGER;

-- Backfill family_id from players
UPDATE active_game_players agp SET family_id = p.family_id
FROM players p WHERE p.id = agp.player_id AND agp.family_id IS NULL;

-- ============================================================
-- 7. Create generic game_scores table
-- ============================================================
CREATE TABLE IF NOT EXISTS game_scores (
    id SERIAL PRIMARY KEY,
    active_game_id INTEGER NOT NULL,
    player_id INTEGER NOT NULL,
    round_number INTEGER NOT NULL,
    score INTEGER NOT NULL,
    metadata JSONB,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT game_scores_unique UNIQUE (active_game_id, player_id, round_number),
    CONSTRAINT game_scores_active_game_fkey FOREIGN KEY (active_game_id) REFERENCES active_games(id) ON DELETE CASCADE,
    CONSTRAINT game_scores_player_fkey FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_game_scores_active_game ON game_scores(active_game_id);
CREATE INDEX IF NOT EXISTS idx_game_scores_player ON game_scores(player_id);

-- Migrate Five Crowns scores into game_scores
INSERT INTO game_scores (active_game_id, player_id, round_number, score)
SELECT active_game_id, player_id, round_number, score
FROM five_crowns_scores
ON CONFLICT (active_game_id, player_id, round_number) DO NOTHING;

-- ============================================================
-- 8. Insert game_details for UNO Classic
-- ============================================================
INSERT INTO game_details (game_id, rules, notes, description_long, min_players, max_players,
    estimated_duration_minutes, difficulty_level, age_recommendation, game_type,
    scoring_system, winning_conditions)
SELECT
    (SELECT id FROM games WHERE slug = 'uno-classic'),
    'Official UNO Rules',
    '<h4>UNO Classic Rules</h4>
<p><strong>Contents:</strong> 108 cards: 19 each of Blue, Green, Red, Yellow (0-9), 8 Draw Two, 8 Reverse, 8 Skip, 4 Wild, 4 Wild Draw Four</p>

<h5>Object</h5>
<p>Be first to discard all cards. Score points for opponents'' remaining cards. First to 500 points wins (or play single rounds).</p>

<h5>Setup</h5>
<ul>
<li>Deal 7 cards each</li>
<li>Rest face down (Draw pile), top card face up (Discard pile)</li>
</ul>

<h5>How to Play</h5>
<p>Match top Discard card by color, number, or symbol. No match? Draw 1 (play if possible). Play proceeds clockwise.</p>

<h5>Action Cards</h5>
<table class="table table-sm table-bordered">
<thead class="table-dark"><tr><th>Card</th><th>Effect</th></tr></thead>
<tbody>
<tr><td>Draw Two</td><td>Next draws 2, skips turn</td></tr>
<tr><td>Reverse</td><td>Reverses direction (acts as Skip in 2-player)</td></tr>
<tr><td>Skip</td><td>Next player skipped</td></tr>
<tr><td>Wild</td><td>Choose color, play anytime</td></tr>
<tr><td>Wild Draw Four</td><td>Choose color; next draws 4, skips. Challengeable.</td></tr>
</tbody>
</table>

<h5>Going Out / UNO!</h5>
<p>Play next-to-last card → yell "UNO!". Caught without? Draw 2. First out ends round.</p>

<h5>Scoring</h5>
<table class="table table-sm table-bordered">
<thead class="table-dark"><tr><th>Cards</th><th>Points</th></tr></thead>
<tbody>
<tr><td>Numbers (0-9)</td><td>Face value</td></tr>
<tr><td>Draw Two / Reverse / Skip</td><td>20</td></tr>
<tr><td>Wild / Wild Draw Four</td><td>50</td></tr>
</tbody>
</table>',
    'The classic card game of matching colors and numbers. Be the first to empty your hand!',
    2, 10, 30, 'Easy', '7+', 'Card Game',
    'Winner scores points from opponents'' remaining cards. Numbers = face value, Action cards = 20 pts, Wilds = 50 pts.',
    'First player to reach the target score (default 500) wins. Or play single rounds - lowest remaining cards wins.'
WHERE NOT EXISTS (SELECT 1 FROM game_details WHERE game_id = (SELECT id FROM games WHERE slug = 'uno-classic'));

-- ============================================================
-- 9. Insert game_details for UNO Flip
-- ============================================================
INSERT INTO game_details (game_id, rules, notes, description_long, min_players, max_players,
    estimated_duration_minutes, difficulty_level, age_recommendation, game_type,
    scoring_system, winning_conditions)
SELECT
    (SELECT id FROM games WHERE slug = 'uno-flip'),
    'Official UNO Flip Rules',
    '<h4>UNO Flip Rules</h4>
<p><strong>Contents:</strong> 112 double-sided cards.</p>

<h5>Light Side (White border)</h5>
<p>Blue/Green/Red/Yellow numbers 1-9; Draw One (8), Reverse (8), Skip (8), Flip (8); Wild (4), Wild Draw Two (4).</p>

<h5>Dark Side (Black border)</h5>
<p>Pink/Teal/Orange/Purple numbers 1-9; Draw Five (8), Reverse (8), Skip Everyone (8), Flip (8); Wild (4), Wild Draw Color (4).</p>

<h5>Object</h5>
<p>Discard all cards. Score for opponents'' cards (side-dependent). First to 500 wins.</p>

<h5>Key Mechanic: Flip!</h5>
<p>When a Flip card is played, ALL cards flip - discard pile, draw pile, and all hands change from Light to Dark side or vice versa.</p>

<h5>Light Side Actions</h5>
<table class="table table-sm table-bordered">
<thead class="table-dark"><tr><th>Card</th><th>Effect</th></tr></thead>
<tbody>
<tr><td>Draw One</td><td>Next draws 1, skips</td></tr>
<tr><td>Reverse</td><td>Reverse direction</td></tr>
<tr><td>Skip</td><td>Next skipped</td></tr>
<tr><td>Wild</td><td>Choose color</td></tr>
<tr><td>Wild Draw Two</td><td>Choose color; next draws 2, skips. Challengeable.</td></tr>
<tr><td>Flip</td><td>Flip to Dark Side (all piles/hands)</td></tr>
</tbody>
</table>

<h5>Dark Side Actions</h5>
<table class="table table-sm table-bordered">
<thead class="table-dark"><tr><th>Card</th><th>Effect</th></tr></thead>
<tbody>
<tr><td>Draw Five</td><td>Next draws 5, skips</td></tr>
<tr><td>Reverse</td><td>Reverse direction</td></tr>
<tr><td>Skip Everyone</td><td>All skipped; back to you</td></tr>
<tr><td>Wild</td><td>Choose color</td></tr>
<tr><td>Wild Draw Color</td><td>Next draws until your color; skips. Challengeable.</td></tr>
<tr><td>Flip</td><td>Flip to Light Side</td></tr>
</tbody>
</table>

<h5>Scoring</h5>
<div class="row">
<div class="col-md-6">
<h6>Light Side Points</h6>
<table class="table table-sm table-bordered">
<thead class="table-dark"><tr><th>Card</th><th>Points</th></tr></thead>
<tbody>
<tr><td>Numbers (1-9)</td><td>Face value</td></tr>
<tr><td>Draw One</td><td>10</td></tr>
<tr><td>Reverse / Skip / Flip</td><td>20</td></tr>
<tr><td>Wild</td><td>40</td></tr>
<tr><td>Wild Draw Two</td><td>50</td></tr>
</tbody>
</table>
</div>
<div class="col-md-6">
<h6>Dark Side Points</h6>
<table class="table table-sm table-bordered">
<thead class="table-dark"><tr><th>Card</th><th>Points</th></tr></thead>
<tbody>
<tr><td>Numbers (1-9)</td><td>Face value</td></tr>
<tr><td>Draw Five</td><td>20</td></tr>
<tr><td>Reverse / Flip</td><td>20</td></tr>
<tr><td>Skip Everyone</td><td>30</td></tr>
<tr><td>Wild</td><td>40</td></tr>
<tr><td>Wild Draw Color</td><td>60</td></tr>
</tbody>
</table>
</div>
</div>

<h5>Key Differences from Classic</h5>
<p>Dual sides (flip changes colors/actions/penalties); no 0s; harsher Dark penalties; score by ending side.</p>',
    'UNO with a twist! Double-sided cards flip between Light and Dark sides with increasingly harsh penalties.',
    2, 10, 45, 'Medium', '7+', 'Card Game',
    'Score by card side at end of hand. Light side: 10-50 pts. Dark side: 20-60 pts. See rules for full breakdown.',
    'First player to reach the target score (default 500) wins.'
WHERE NOT EXISTS (SELECT 1 FROM game_details WHERE game_id = (SELECT id FROM games WHERE slug = 'uno-flip'));

-- ============================================================
-- 10. Add families FK to active_games
-- ============================================================
ALTER TABLE families ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP;

-- ============================================================
-- Verify migration
-- ============================================================
DO $$
DECLARE
    fc_scores_count INTEGER;
    gs_scores_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO fc_scores_count FROM five_crowns_scores;
    SELECT COUNT(*) INTO gs_scores_count FROM game_scores;
    IF gs_scores_count < fc_scores_count THEN
        RAISE EXCEPTION 'Migration verification failed: game_scores (%) has fewer rows than five_crowns_scores (%)', gs_scores_count, fc_scores_count;
    END IF;
    RAISE NOTICE 'Migration verified: % five_crowns_scores migrated to % game_scores', fc_scores_count, gs_scores_count;
END $$;

COMMIT;
