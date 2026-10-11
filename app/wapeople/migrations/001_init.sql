-- WA People core schema.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- A two-year legislative term, e.g. '2025-26'.
CREATE TABLE bienniums (
    id                 text PRIMARY KEY,
    start_year         int  NOT NULL,
    end_year           int  NOT NULL,
    -- upcoming | in_session | interim | adjourned_final | closed
    status             text NOT NULL DEFAULT 'closed',
    last_activity_date date,
    bills_synced_at    timestamptz,
    details_synced_at  timestamptz
);

-- Legislators. id is the Legislature's own member id (stable across terms).
CREATE TABLE members (
    id              int PRIMARY KEY,
    name            text NOT NULL,
    first_name      text,
    last_name       text,
    agency          text,          -- House | Senate (most recent term)
    party           text,          -- D | R | ... (most recent term)
    district        text,
    email           text,
    phone           text,
    latest_biennium text,
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX members_name_trgm ON members USING gin (name gin_trgm_ops);
CREATE INDEX members_last_name_idx ON members (last_name);

-- Which chamber/party/district a member held in each biennium.
CREATE TABLE member_terms (
    member_id  int  NOT NULL REFERENCES members (id) ON DELETE CASCADE,
    biennium   text NOT NULL REFERENCES bienniums (id),
    agency     text,
    party      text,
    district   text,
    long_name  text,
    acronym    text,
    PRIMARY KEY (member_id, biennium)
);
CREATE INDEX member_terms_biennium_idx ON member_terms (biennium, agency);

-- One row per piece of legislation per biennium (all versions — SHB, ESHB, ... — folded in).
CREATE TABLE bills (
    id                  bigserial PRIMARY KEY,
    biennium            text NOT NULL REFERENCES bienniums (id),
    bill_number         int  NOT NULL,
    type_code           text NOT NULL,   -- B, JR, JM, CR, R, GA, I
    type_name           text NOT NULL,
    bill_id             text NOT NULL,   -- base id, e.g. 'HB 1217'
    current_version     text,            -- e.g. 'EHB 1217'
    versions            text[] NOT NULL DEFAULT '{}',
    original_agency     text,
    short_description   text,
    long_description    text,
    legal_title         text,
    introduced_date     date,
    prime_sponsor_id    int,
    sponsor_text        text,
    request_number      text,
    requested_by        text[] NOT NULL DEFAULT '{}',
    appropriations      boolean NOT NULL DEFAULT false,
    fiscal_note         boolean NOT NULL DEFAULT false,
    companions          text[] NOT NULL DEFAULT '{}',
    status_code         text,
    status_line         text,
    status_date         date,
    vetoed              boolean NOT NULL DEFAULT false,
    partial_veto        boolean NOT NULL DEFAULT false,
    passed_house        boolean NOT NULL DEFAULT false,
    passed_senate       boolean NOT NULL DEFAULT false,
    passed_legislature  boolean NOT NULL DEFAULT false,
    session_law_year    int,
    session_law_chapter int,
    session_law_special int,
    stage               text NOT NULL,
    stage_label         text NOT NULL,
    bucket              text NOT NULL,   -- proposed | ongoing | past
    last_action_date    date,
    details_action_date date,            -- last_action_date when sponsors/votes were fetched
    details_synced_at   timestamptz,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    search tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('simple', coalesce(bill_id, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(short_description, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(long_description, '')), 'B') ||
        setweight(to_tsvector('english', coalesce(legal_title, '')), 'C')
    ) STORED,
    UNIQUE (biennium, bill_number, type_code)
);
CREATE INDEX bills_search_idx ON bills USING gin (search);
CREATE INDEX bills_bucket_idx ON bills (bucket, type_code, last_action_date DESC);
CREATE INDEX bills_stage_idx ON bills (stage);
CREATE INDEX bills_number_idx ON bills (bill_number, biennium);
CREATE INDEX bills_prime_idx ON bills (prime_sponsor_id);
CREATE INDEX bills_last_action_idx ON bills (last_action_date DESC NULLS LAST);
CREATE INDEX bills_session_law_idx ON bills (session_law_year, session_law_chapter);
CREATE INDEX bills_short_desc_trgm ON bills USING gin (short_description gin_trgm_ops);

CREATE TABLE bill_sponsors (
    bill_pk       bigint NOT NULL REFERENCES bills (id) ON DELETE CASCADE,
    member_id     int    NOT NULL,
    sponsor_type  text   NOT NULL,     -- Primary | Secondary
    sponsor_order int,
    name          text,
    PRIMARY KEY (bill_pk, member_id)
);
CREATE INDEX bill_sponsors_member_idx ON bill_sponsors (member_id, sponsor_type);

-- Bill history ("First reading, referred to Housing.", ...)
CREATE TABLE bill_actions (
    id          bigserial PRIMARY KEY,
    bill_pk     bigint NOT NULL REFERENCES bills (id) ON DELETE CASCADE,
    action_date date,
    version_id  text,
    description text NOT NULL,
    seq         int  NOT NULL
);
CREATE INDEX bill_actions_bill_idx ON bill_actions (bill_pk, seq);

-- A recorded floor vote.
CREATE TABLE roll_calls (
    id              bigserial PRIMARY KEY,
    bill_pk         bigint NOT NULL REFERENCES bills (id) ON DELETE CASCADE,
    biennium        text   NOT NULL,
    agency          text   NOT NULL,   -- House | Senate
    version_id      text,
    motion          text,
    sequence_number int,
    vote_date       date,
    yeas            int NOT NULL DEFAULT 0,
    nays            int NOT NULL DEFAULT 0,
    absent          int NOT NULL DEFAULT 0,
    excused         int NOT NULL DEFAULT 0,
    result          text               -- passed | failed | adopted | confirmed | ... (from bill history)
);
CREATE INDEX roll_calls_bill_idx ON roll_calls (bill_pk);
CREATE INDEX roll_calls_date_idx ON roll_calls (vote_date DESC, agency);
CREATE INDEX roll_calls_biennium_idx ON roll_calls (biennium, agency);

CREATE TABLE votes (
    roll_call_id bigint NOT NULL REFERENCES roll_calls (id) ON DELETE CASCADE,
    member_id    int    NOT NULL,
    member_name  text,
    vote         text   NOT NULL,      -- Yea | Nay | Absent | Excused
    PRIMARY KEY (roll_call_id, member_id)
);
CREATE INDEX votes_member_idx ON votes (member_id, roll_call_id);

-- Washington Supreme Court decisions about legislation and public policy.
CREATE TABLE court_cases (
    id                bigserial PRIMARY KEY,
    case_key          text NOT NULL UNIQUE,      -- normalized docket + filing date
    cluster_ids       int[] NOT NULL DEFAULT '{}',
    opinion_ids       int[] NOT NULL DEFAULT '{}',
    case_name         text NOT NULL,
    case_name_full    text,
    date_filed        date,
    docket_number     text,
    citations         text[] NOT NULL DEFAULT '{}',
    cite_count        int NOT NULL DEFAULT 0,
    status            text,
    judges            text,
    courtlistener_url text,
    pdf_url           text,
    categories        text[] NOT NULL DEFAULT '{}',
    snippets          jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {category: highlighted excerpt}
    snippet_text      text,
    full_text         text,
    full_text_source  text,
    bill_mentions     jsonb NOT NULL DEFAULT '[]'::jsonb,
    text_checked_at   timestamptz,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    search tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(case_name, '')), 'A') ||
        setweight(to_tsvector('simple', coalesce(docket_number, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(snippet_text, '')), 'B') ||
        setweight(to_tsvector('english', left(coalesce(full_text, ''), 400000)), 'D')
    ) STORED
);
CREATE INDEX court_cases_search_idx ON court_cases USING gin (search);
CREATE INDEX court_cases_date_idx ON court_cases (date_filed DESC);
CREATE INDEX court_cases_categories_idx ON court_cases USING gin (categories);
CREATE INDEX court_cases_name_trgm ON court_cases USING gin (case_name gin_trgm_ops);

CREATE TABLE case_bill_links (
    case_id   bigint NOT NULL REFERENCES court_cases (id) ON DELETE CASCADE,
    bill_pk   bigint NOT NULL REFERENCES bills (id) ON DELETE CASCADE,
    link_type text   NOT NULL,   -- session_law | bill_number
    mention   text,
    PRIMARY KEY (case_id, bill_pk)
);
CREATE INDEX case_bill_links_bill_idx ON case_bill_links (bill_pk);

-- Audit trail of every data refresh.
CREATE TABLE sync_runs (
    id          bigserial PRIMARY KEY,
    job         text NOT NULL,
    scope       text,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status      text NOT NULL DEFAULT 'running',   -- running | ok | error
    stats       jsonb,
    error       text
);
CREATE INDEX sync_runs_job_idx ON sync_runs (job, started_at DESC);
