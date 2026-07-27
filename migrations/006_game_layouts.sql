-- Migration 006: per-family, per-game saved layouts (players + settings).
-- Apply after backup. Safe to re-run (IF NOT EXISTS / ON CONFLICT).

CREATE TABLE IF NOT EXISTS game_layouts (
    id SERIAL PRIMARY KEY,
    family_id INTEGER NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    name VARCHAR(80) NOT NULL,
    player_ids JSONB NOT NULL,
    scoring_direction VARCHAR(20),
    target_score INTEGER,
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT game_layouts_name_unique UNIQUE (family_id, game_id, name)
);

CREATE INDEX IF NOT EXISTS idx_game_layouts_family_game
    ON game_layouts (family_id, game_id);

-- At most one default layout per family + game type.
CREATE UNIQUE INDEX IF NOT EXISTS idx_game_layouts_one_default
    ON game_layouts (family_id, game_id)
    WHERE is_default = TRUE;

-- Seed DiFede Five Crowns "Kim & Joe" as the default layout when those
-- roster members still exist. Skips quietly if already seeded or missing.
INSERT INTO game_layouts (family_id, game_id, name, player_ids, is_default, scoring_direction, target_score)
SELECT f.id, g.id, 'Kim & Joe',
       jsonb_build_array(kim.id, joe.id),
       TRUE, NULL, NULL
FROM families f
CROSS JOIN games g
JOIN players kim ON kim.family_id = f.id AND kim.display_name = 'Kim'
    AND kim.archived_at IS NULL AND kim.purged_at IS NULL
JOIN players joe ON joe.family_id = f.id AND joe.display_name = 'Joe'
    AND joe.archived_at IS NULL AND joe.purged_at IS NULL
WHERE f.name = 'DiFede' AND f.archived_at IS NULL
  AND g.slug = 'five-crowns'
ON CONFLICT (family_id, game_id, name) DO NOTHING;
