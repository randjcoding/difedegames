-- Migration 004: identity rebuild
-- Family slugs + privacy, player email/privacy/minor protection, lifecycle
-- (archive/reinstate/purge), release requests, action tokens, invitations,
-- and FK hardening so no single delete can destroy game history.
-- Idempotent: safe to run multiple times.

BEGIN;

-- 1. Families: allow duplicate names, add stable handle + privacy ----------
ALTER TABLE families DROP CONSTRAINT IF EXISTS families_name_key;
ALTER TABLE families ADD COLUMN IF NOT EXISTS slug varchar(120);
ALTER TABLE families ADD COLUMN IF NOT EXISTS is_discoverable boolean NOT NULL DEFAULT true;
ALTER TABLE families ADD COLUMN IF NOT EXISTS show_roster     boolean NOT NULL DEFAULT true;

UPDATE families
SET slug = lower(regexp_replace(trim(name), '[^a-zA-Z0-9]+', '-', 'g'))
WHERE slug IS NULL;

UPDATE families f SET slug = f.slug || '-' || f.id
WHERE EXISTS (SELECT 1 FROM families g WHERE g.slug = f.slug AND g.id < f.id);

ALTER TABLE families ALTER COLUMN slug SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS families_slug_key ON families (slug);

-- 2. Players: verified email + privacy + minor protection ------------------
ALTER TABLE players ADD COLUMN IF NOT EXISTS email_verified      boolean NOT NULL DEFAULT false;
ALTER TABLE players ADD COLUMN IF NOT EXISTS is_discoverable     boolean NOT NULL DEFAULT true;
ALTER TABLE players ADD COLUMN IF NOT EXISTS show_full_last_name boolean NOT NULL DEFAULT false;
ALTER TABLE players ADD COLUMN IF NOT EXISTS is_minor            boolean NOT NULL DEFAULT false;

-- Email is OPTIONAL on players. This index only constrains rows that have one,
-- and deliberately spans archived rows so an identity can never be taken over.
CREATE UNIQUE INDEX IF NOT EXISTS players_email_unique
    ON players (lower(email)) WHERE email IS NOT NULL AND email <> '';

-- 2b. Disambiguate identity from provenance --------------------------------
-- players.user_id means "account that created this row", NOT "this person's
-- account". users.player_id is the real identity link. Rename so the two can
-- never be confused again.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'players' AND column_name = 'user_id')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'players' AND column_name = 'created_by_user_id')
    THEN
        ALTER TABLE players RENAME COLUMN user_id TO created_by_user_id;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS users_one_person_each
    ON users (player_id) WHERE player_id IS NOT NULL;

-- 3. Release / transfer requests (batched) ---------------------------------
CREATE TABLE IF NOT EXISTS player_release_requests (
    id                   serial PRIMARY KEY,
    batch_id             uuid NOT NULL,
    player_id            integer NOT NULL REFERENCES players(id)  ON DELETE CASCADE,
    from_family_id       integer NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    to_family_id         integer NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    requested_by_user_id integer NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
    note                 text,
    status               varchar(20) NOT NULL DEFAULT 'pending',
    decided_by_user_id   integer REFERENCES users(id),
    decided_at           timestamp,
    created_at           timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT prr_status_check CHECK (status IN ('pending','approved','denied','cancelled'))
);
CREATE UNIQUE INDEX IF NOT EXISTS prr_one_open_per_player
    ON player_release_requests (player_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS prr_from_family ON player_release_requests (from_family_id, status);
CREATE INDEX IF NOT EXISTS prr_batch       ON player_release_requests (batch_id);

-- 4. Single-use action tokens (email approve/claim links) ------------------
CREATE TABLE IF NOT EXISTS action_tokens (
    id         serial PRIMARY KEY,
    token      varchar(128) NOT NULL UNIQUE,
    purpose    varchar(40)  NOT NULL,
    player_id  integer REFERENCES players(id)  ON DELETE CASCADE,
    user_id    integer REFERENCES users(id)    ON DELETE CASCADE,
    family_id  integer REFERENCES families(id) ON DELETE CASCADE,
    payload    jsonb NOT NULL DEFAULT '{}',
    expires_at timestamp NOT NULL,
    used_at    timestamp,
    created_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT action_tokens_purpose_check
        CHECK (purpose IN ('claim_profile','approve_release','join_family','family_invite','reinstate_claim'))
);
CREATE INDEX IF NOT EXISTS action_tokens_lookup ON action_tokens (token) WHERE used_at IS NULL;

-- 5. Invitations (email invites) -------------------------------------------
CREATE TABLE IF NOT EXISTS invitations (
    id                 serial PRIMARY KEY,
    email              varchar(255) NOT NULL,
    invited_by_user_id integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    family_id          integer REFERENCES families(id) ON DELETE SET NULL,
    player_id          integer REFERENCES players(id)  ON DELETE SET NULL,
    invite_type        varchar(20) NOT NULL,
    token              varchar(128) NOT NULL UNIQUE,
    status             varchar(20) NOT NULL DEFAULT 'sent',
    expires_at         timestamp NOT NULL,
    accepted_at        timestamp,
    created_at         timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT invitations_type_check
        CHECK (invite_type IN ('join_family','join_site','claim_profile')),
    CONSTRAINT invitations_status_check
        CHECK (status IN ('sent','accepted','expired','revoked'))
);
CREATE INDEX IF NOT EXISTS invitations_email  ON invitations (lower(email), status);
CREATE INDEX IF NOT EXISTS invitations_sender ON invitations (invited_by_user_id, created_at);

-- 6. Lifecycle columns (archive / reinstate / purge) -----------------------
ALTER TABLE players  ADD COLUMN IF NOT EXISTS archived_at         timestamp;
ALTER TABLE players  ADD COLUMN IF NOT EXISTS archived_by_user_id integer REFERENCES users(id);
ALTER TABLE players  ADD COLUMN IF NOT EXISTS archive_reason      text;
ALTER TABLE players  ADD COLUMN IF NOT EXISTS purged_at           timestamp;

ALTER TABLE families ADD COLUMN IF NOT EXISTS archived_at         timestamp;
ALTER TABLE families ADD COLUMN IF NOT EXISTS archived_by_user_id integer REFERENCES users(id);

ALTER TABLE users    ADD COLUMN IF NOT EXISTS archived_at         timestamp;
ALTER TABLE users    ADD COLUMN IF NOT EXISTS archived_by_user_id integer REFERENCES users(id);

CREATE INDEX IF NOT EXISTS players_active_idx  ON players  (id) WHERE archived_at IS NULL;
CREATE INDEX IF NOT EXISTS families_active_idx ON families (id) WHERE archived_at IS NULL;

-- 7. FK hardening: fail loudly, never silently destroy history -------------
-- active_games.user_id was ON DELETE CASCADE: deleting user 1 would destroy
-- 494 games and 11,674 scores. Games belong to a FAMILY, not to the account
-- that happened to create them, so detach instead of cascade.
ALTER TABLE active_games DROP CONSTRAINT IF EXISTS fk_active_games_user_id;
ALTER TABLE active_games ADD  CONSTRAINT fk_active_games_user_id
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;

-- Score history must never disappear because of a person row.
ALTER TABLE game_scores DROP CONSTRAINT IF EXISTS game_scores_player_fkey;
ALTER TABLE game_scores ADD  CONSTRAINT game_scores_player_fkey
    FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE RESTRICT;

ALTER TABLE active_game_players DROP CONSTRAINT IF EXISTS active_game_players_player_id_fkey;
ALTER TABLE active_game_players ADD  CONSTRAINT active_game_players_player_id_fkey
    FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE RESTRICT;

ALTER TABLE five_crowns_scores DROP CONSTRAINT IF EXISTS five_crowns_scores_player_id_fkey;
ALTER TABLE five_crowns_scores ADD  CONSTRAINT five_crowns_scores_player_id_fkey
    FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE RESTRICT;

-- winner_id must be nullable for SET NULL to be legal.
ALTER TABLE game_stats ALTER COLUMN winner_id DROP NOT NULL;
ALTER TABLE game_stats DROP CONSTRAINT IF EXISTS game_stats_winner_id_fkey;
ALTER TABLE game_stats ADD  CONSTRAINT game_stats_winner_id_fkey
    FOREIGN KEY (winner_id) REFERENCES players(id) ON DELETE SET NULL;

COMMIT;
