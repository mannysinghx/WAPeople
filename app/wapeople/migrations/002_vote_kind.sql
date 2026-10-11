-- Classify each roll call (final passage, concurrence, amendment, ...) and keep the
-- bill-history line it was matched to, since motion text is inconsistent across years.
ALTER TABLE roll_calls
    ADD COLUMN vote_kind    text NOT NULL DEFAULT 'procedural',
    ADD COLUMN history_line text;
CREATE INDEX roll_calls_kind_date_idx ON roll_calls (vote_kind, vote_date DESC);
