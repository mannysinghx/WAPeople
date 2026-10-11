-- Resumable court collection: CourtListener limits anonymous use (50/hour plus a daily cap),
-- so the full backfill may span several runs. One row per category query and start date.
CREATE TABLE court_sync_state (
    category       text NOT NULL,
    filed_after    text NOT NULL,
    next_url       text,              -- cursor to resume from; NULL = not started or finished
    pages          int  NOT NULL DEFAULT 0,
    hits           int  NOT NULL DEFAULT 0,
    started_at     timestamptz,
    completed_at   timestamptz,
    PRIMARY KEY (category, filed_after)
);

-- When CourtListener asks us to back off for a long time, remember until when.
CREATE TABLE court_rate_limit (
    id          int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    retry_after timestamptz NOT NULL
);
