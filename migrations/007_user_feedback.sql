-- Migration 007: user feedback / bug / enhancement / new-game requests.
-- Notifies all super_admins. Tracks who has opened each item (paper trail).
-- Apply after backup. Safe to re-run (IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS feedback_items (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category VARCHAR(40) NOT NULL,
    subject VARCHAR(200) NOT NULL,
    body TEXT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'open',
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT feedback_items_category_check CHECK (category::text = ANY (ARRAY[
        'bug'::character varying,
        'enhancement'::character varying,
        'new_game'::character varying,
        'feedback'::character varying,
        'other'::character varying
    ]::text[])),
    CONSTRAINT feedback_items_status_check CHECK (status::text = ANY (ARRAY[
        'open'::character varying,
        'in_progress'::character varying,
        'closed'::character varying
    ]::text[]))
);

CREATE INDEX IF NOT EXISTS idx_feedback_items_status_created
    ON feedback_items (status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_feedback_items_user
    ON feedback_items (user_id);

CREATE TABLE IF NOT EXISTS feedback_views (
    feedback_id INTEGER NOT NULL REFERENCES feedback_items(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    first_seen_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (feedback_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_feedback_views_user
    ON feedback_views (user_id);
