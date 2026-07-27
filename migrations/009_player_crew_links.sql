-- Migration 009: person-to-person crew links (individual crew-up).
-- Separate from whole-family family_alliances. Invitee must accept.
-- Apply after backup. Safe to re-run (IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS player_crew_links (
    id SERIAL PRIMARY KEY,
    player_a_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    player_b_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    requested_by_player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    responded_by_player_id INTEGER REFERENCES players(id) ON DELETE SET NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT player_crew_links_ordered CHECK (player_a_id < player_b_id),
    CONSTRAINT player_crew_links_distinct CHECK (player_a_id <> player_b_id),
    CONSTRAINT player_crew_links_pair UNIQUE (player_a_id, player_b_id),
    CONSTRAINT player_crew_links_status_check CHECK (status::text = ANY (ARRAY[
        'pending'::character varying,
        'accepted'::character varying,
        'declined'::character varying,
        'ended'::character varying
    ]::text[]))
);

CREATE INDEX IF NOT EXISTS idx_player_crew_links_a_status
    ON player_crew_links (player_a_id, status);
CREATE INDEX IF NOT EXISTS idx_player_crew_links_b_status
    ON player_crew_links (player_b_id, status);
CREATE INDEX IF NOT EXISTS idx_player_crew_links_requested
    ON player_crew_links (requested_by_player_id);
