-- Migration 005: remember when a lead decided two similar players are NOT
-- the same person, so "Possible duplicates" stops suggesting them.
-- Apply after backup. Safe to re-run (IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS player_not_duplicates (
    player_a_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    player_b_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    decided_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT player_not_duplicates_ordered CHECK (player_a_id < player_b_id),
    CONSTRAINT player_not_duplicates_pkey PRIMARY KEY (player_a_id, player_b_id)
);

CREATE INDEX IF NOT EXISTS idx_player_not_duplicates_b
    ON player_not_duplicates (player_b_id);

-- Allow password-setup invites for existing roster members (no new players row).
ALTER TABLE invitations DROP CONSTRAINT IF EXISTS invitations_type_check;
ALTER TABLE invitations ADD CONSTRAINT invitations_type_check
    CHECK (invite_type::text = ANY (ARRAY[
        'join_family'::character varying,
        'join_site'::character varying,
        'claim_profile'::character varying,
        'set_password'::character varying
    ]::text[]));

ALTER TABLE action_tokens DROP CONSTRAINT IF EXISTS action_tokens_purpose_check;
ALTER TABLE action_tokens ADD CONSTRAINT action_tokens_purpose_check
    CHECK (purpose::text = ANY (ARRAY[
        'claim_profile'::character varying,
        'approve_release'::character varying,
        'join_family'::character varying,
        'family_invite'::character varying,
        'reinstate_claim'::character varying,
        'set_password'::character varying
    ]::text[]));
