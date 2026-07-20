-- Migration 002: Identity / membership foundation (Phase 1)
--
-- Non-breaking schema groundwork for multi-family membership. This ONLY adds
-- structures and backfills them from existing data. It changes no application
-- behavior; existing columns (players.family_id, users.family_id) remain the
-- cached "primary family" until later phases read from memberships directly.
--
-- Model:
--   player   = a person's canonical identity and lifetime stat owner
--              (game_scores.player_id already points here)
--   families.lead_user_id = explicit, transferable family leadership
--   player_family_memberships = which families a person belongs to, with
--                               exactly one primary; host family for a given
--                               game is still recorded on active_games.family_id
--
-- Idempotent: safe to run more than once and safe for docker initdb.

BEGIN;

-- 1. Explicit, transferable family leadership -----------------------------
ALTER TABLE families ADD COLUMN IF NOT EXISTS lead_user_id integer REFERENCES users(id);

-- Backfill leadership: prefer the creator (if still a user), otherwise the
-- earliest admin in the family, otherwise the earliest member.
UPDATE families f
SET lead_user_id = sub.uid
FROM (
    SELECT f2.id AS fid,
           COALESCE(
             (SELECT u.id FROM users u WHERE u.id = f2.created_by_user_id),
             (SELECT u.id FROM users u
                WHERE u.family_id = f2.id AND u.role IN ('super_admin', 'family_admin')
                ORDER BY u.created_at ASC, u.id ASC LIMIT 1),
             (SELECT u.id FROM users u
                WHERE u.family_id = f2.id
                ORDER BY u.created_at ASC, u.id ASC LIMIT 1)
           ) AS uid
    FROM families f2
) sub
WHERE f.id = sub.fid AND f.lead_user_id IS NULL;

-- 2. Person <-> family membership -----------------------------------------
CREATE TABLE IF NOT EXISTS player_family_memberships (
    id          serial PRIMARY KEY,
    player_id   integer NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    family_id   integer NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    is_primary  boolean NOT NULL DEFAULT false,
    status      varchar(20) NOT NULL DEFAULT 'active',
    role        varchar(20) NOT NULL DEFAULT 'member',
    joined_at   timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT player_family_memberships_status_check
        CHECK (status IN ('active', 'invited', 'requested', 'removed')),
    CONSTRAINT player_family_memberships_role_check
        CHECK (role IN ('member', 'lead')),
    UNIQUE (player_id, family_id)
);

-- Enforce exactly one primary family per person.
CREATE UNIQUE INDEX IF NOT EXISTS one_primary_family_per_player
    ON player_family_memberships (player_id) WHERE is_primary;

CREATE INDEX IF NOT EXISTS idx_pfm_family_status
    ON player_family_memberships (family_id, status);
CREATE INDEX IF NOT EXISTS idx_pfm_player
    ON player_family_memberships (player_id);

-- 3. Backfill each existing player's single family as their primary --------
INSERT INTO player_family_memberships (player_id, family_id, is_primary, status, role)
SELECT p.id, p.family_id, TRUE, 'active', 'member'
FROM players p
WHERE p.family_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM player_family_memberships m WHERE m.player_id = p.id
  );

COMMIT;
