-- Federal campaign money from the Federal Election Commission's public bulk files. Federal and state
-- rules differ, so these are separate tables and the pages label every figure with its source.
--
-- Privacy: the FEC's transaction files have no street addresses, its committee file's addresses and
-- treasurer names aren't copied, and donor ZIP codes are cut to five digits.

ALTER TABLE money_people
    ADD COLUMN fec_candidate_ids text[] NOT NULL DEFAULT '{}',
    ADD COLUMN fed_raised        numeric(16,2) NOT NULL DEFAULT 0,
    ADD COLUMN fed_spent         numeric(16,2) NOT NULL DEFAULT 0,
    ADD COLUMN fed_cycles        int NOT NULL DEFAULT 0,
    ADD COLUMN fed_first_cycle   int,
    ADD COLUMN fed_last_cycle    int,
    ADD COLUMN fed_latest_raised numeric(16,2),
    ADD COLUMN fed_cash_on_hand  numeric(16,2),
    ADD COLUMN fed_debts         numeric(16,2),
    ADD COLUMN fed_contributions int NOT NULL DEFAULT 0,
    ADD COLUMN fed_in_state      numeric(16,2) NOT NULL DEFAULT 0,
    ADD COLUMN fed_out_of_state  numeric(16,2) NOT NULL DEFAULT 0,
    ADD COLUMN fed_top_source    text,
    ADD COLUMN fed_rollup        jsonb NOT NULL DEFAULT '{}'::jsonb;

-- What the FEC itself reports for each candidate in each two-year period (its "all candidates" file).
CREATE TABLE fec_cycles (
    candidate_id     text NOT NULL,
    cycle            int  NOT NULL,
    person_pk        int  NOT NULL REFERENCES money_people (id) ON DELETE CASCADE,
    candidate_name   text,
    office           text,                    -- H | S
    state            text,
    district         text,
    party            text,
    receipts         numeric(16,2) NOT NULL DEFAULT 0,
    disbursements    numeric(16,2) NOT NULL DEFAULT 0,
    from_individuals numeric(16,2) NOT NULL DEFAULT 0,
    from_pacs        numeric(16,2) NOT NULL DEFAULT 0,
    from_parties     numeric(16,2) NOT NULL DEFAULT 0,
    candidate_contributions numeric(16,2) NOT NULL DEFAULT 0,
    candidate_loans  numeric(16,2) NOT NULL DEFAULT 0,
    other_loans      numeric(16,2) NOT NULL DEFAULT 0,
    transfers_in     numeric(16,2) NOT NULL DEFAULT 0,
    transfers_out    numeric(16,2) NOT NULL DEFAULT 0,
    individual_refunds numeric(16,2) NOT NULL DEFAULT 0,
    committee_refunds  numeric(16,2) NOT NULL DEFAULT 0,
    cash_start       numeric(16,2),
    cash_end         numeric(16,2),
    debts            numeric(16,2),
    coverage_end     date,
    -- Sums of the itemized rows below, kept next to the reported figures so differences are visible.
    itemized_individuals numeric(16,2),
    itemized_committees  numeric(16,2),
    itemized_operating   numeric(16,2),
    PRIMARY KEY (candidate_id, cycle)
);
CREATE INDEX fec_cycles_person_idx ON fec_cycles (person_pk, cycle DESC);

CREATE TABLE fec_committees (
    committee_id  text NOT NULL,
    cycle         int  NOT NULL,
    person_pk     int  NOT NULL REFERENCES money_people (id) ON DELETE CASCADE,
    candidate_id  text NOT NULL,
    name          text,
    designation   text,                       -- P principal | A authorized | D leadership | J joint
    is_campaign   boolean NOT NULL,           -- only campaign committees' money is itemized here
    PRIMARY KEY (committee_id, cycle)
);
CREATE INDEX fec_committees_person_idx ON fec_committees (person_pk, cycle DESC);

-- Money in: itemized individuals, and committees' gifts as reported by the committee that gave.
CREATE TABLE fec_contributions (
    sub_id        bigint PRIMARY KEY,
    person_pk     int  NOT NULL REFERENCES money_people (id) ON DELETE CASCADE,
    committee_id  text NOT NULL,              -- the campaign committee receiving it
    cycle         int  NOT NULL,
    dataset       text NOT NULL,              -- indiv | oth | pas2
    tx_type       text,
    report_type   text,
    election      text,
    image_num     text,
    source_type   text NOT NULL,
    is_memo       boolean NOT NULL,           -- memo entries don't add to totals
    is_refund     boolean NOT NULL,
    name          text NOT NULL,
    city          text,
    state         text,
    zip           text,
    employer      text,
    occupation    text,
    tx_date       date,
    amount        numeric(14,2) NOT NULL,     -- negative for refunds
    other_id      text,                       -- another committee involved, such as a conduit or giver
    connected_org text,
    memo_text     text
);
CREATE INDEX fec_contrib_person_date_idx ON fec_contributions (person_pk, tx_date DESC NULLS LAST, sub_id DESC);
CREATE INDEX fec_contrib_person_amount_idx ON fec_contributions (person_pk, amount DESC);
CREATE INDEX fec_contrib_load_idx ON fec_contributions (cycle, dataset);

-- Money out: operating expenses, and payments to other committees.
CREATE TABLE fec_disbursements (
    sub_id        bigint PRIMARY KEY,
    person_pk     int  NOT NULL REFERENCES money_people (id) ON DELETE CASCADE,
    committee_id  text NOT NULL,
    cycle         int  NOT NULL,
    dataset       text NOT NULL,              -- oppexp | oth
    tx_type       text,
    report_type   text,
    image_num     text,
    category      text NOT NULL,
    purpose       text,
    is_memo       boolean NOT NULL,
    name          text,
    city          text,
    state         text,
    zip           text,
    tx_date       date,
    amount        numeric(14,2) NOT NULL,
    other_id      text
);
CREATE INDEX fec_disb_person_date_idx ON fec_disbursements (person_pk, tx_date DESC NULLS LAST, sub_id DESC);
CREATE INDEX fec_disb_person_amount_idx ON fec_disbursements (person_pk, amount DESC);
CREATE INDEX fec_disb_load_idx ON fec_disbursements (cycle, dataset);

-- Outside money: independent expenditures that name the candidate.
CREATE TABLE fec_outside (
    id            text PRIMARY KEY,
    person_pk     int  NOT NULL REFERENCES money_people (id) ON DELETE CASCADE,
    candidate_id  text NOT NULL,
    cycle         int  NOT NULL,
    spender_id    text,
    spender_name  text,
    support_oppose text,                      -- S | O
    amount        numeric(14,2) NOT NULL,
    tx_date       date,
    purpose       text,
    payee         text,
    image_num     text
);
CREATE INDEX fec_outside_person_idx ON fec_outside (person_pk, cycle DESC, amount DESC);
CREATE INDEX fec_outside_load_idx ON fec_outside (cycle);

-- Which version of each public file is loaded, so unchanged files are never read again. Rows are
-- filtered to the roster as they're read, so a changed roster (candidate id:person id) means a reload.
CREATE TABLE fec_sync_state (
    dataset    text NOT NULL,
    cycle      int  NOT NULL,
    etag       text,
    rows       int  NOT NULL DEFAULT 0,
    roster     text[] NOT NULL DEFAULT '{}',
    loaded_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (dataset, cycle)
);
