-- Migration 008: feedback reply threads.
-- Apply after backup. Safe to re-run (IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS feedback_replies (
    id SERIAL PRIMARY KEY,
    feedback_id INTEGER NOT NULL REFERENCES feedback_items(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_feedback_replies_feedback_created
    ON feedback_replies (feedback_id, created_at);
