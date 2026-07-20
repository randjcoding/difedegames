-- Migration 003: Identity links + primary-family sync
--
-- Builds on 002. Adds the account <-> identity link and keeps the legacy
-- players.family_id column automatically in sync with the primary membership,
-- so existing "home family" reads stay correct while memberships drive
-- multi-family rosters.
--
-- Idempotent.

BEGIN;

-- 1. Account -> its own player identity (stat profile) --------------------
--    Distinct from players.user_id, which historically means "managing
--    account". This column means "this login IS this person".
ALTER TABLE users ADD COLUMN IF NOT EXISTS player_id integer REFERENCES players(id) ON DELETE SET NULL;

-- Backfill by unambiguous name match within the user's family.
UPDATE users u
SET player_id = m.pid
FROM (
    SELECT u2.id AS uid,
           (SELECT p.id
              FROM players p
              JOIN player_family_memberships pm
                ON pm.player_id = p.id AND pm.family_id = u2.family_id
             WHERE lower(p.first_name) = lower(u2.first_name)
             ORDER BY p.id
             LIMIT 1) AS pid
    FROM users u2
) m
WHERE u.id = m.uid AND m.pid IS NOT NULL AND u.player_id IS NULL;

-- 2. Keep players.family_id (cached "home family") in step with primary ----
CREATE OR REPLACE FUNCTION sync_player_primary_family() RETURNS trigger AS $$
BEGIN
    IF NEW.is_primary THEN
        UPDATE players
        SET family_id = NEW.family_id
        WHERE id = NEW.player_id AND family_id IS DISTINCT FROM NEW.family_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_sync_player_primary_family ON player_family_memberships;
CREATE TRIGGER trg_sync_player_primary_family
    AFTER INSERT OR UPDATE OF is_primary, family_id ON player_family_memberships
    FOR EACH ROW EXECUTE FUNCTION sync_player_primary_family();

COMMIT;
