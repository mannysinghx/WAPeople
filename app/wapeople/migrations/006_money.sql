-- Where elected officials' money comes from and where it goes, from the Washington Public
-- Disclosure Commission's open data (data.wa.gov). Everything here is derived data: the sync can
-- rebuild it from scratch, and nothing outside these tables depends on it.
--
-- Privacy: street addresses, phone numbers and emails are never fetched or stored, even though
-- the source publishes them. Donors appear with city, state and ZIP.

-- One elected official. id is members.id for legislators (so links stay stable) and
-- 1000000 + the PDC person id for officials who never served in the Legislature.
CREATE TABLE money_people (
    id              int PRIMARY KEY,
    member_id       int UNIQUE REFERENCES members (id) ON DELETE SET NULL,
    name            text NOT NULL,
    kind            text NOT NULL,      -- legislator | statewide | justice
    title           text,               -- e.g. 'State Senator, District 3', 'Governor'
    party           text,
    chamber         text,
    district        text,
    is_current      boolean NOT NULL DEFAULT false,
    pdc_person_ids  int[] NOT NULL DEFAULT '{}',
    link_method     text,               -- how this person was matched to PDC records
    link_note       text,
    source_url      text,               -- official roster page, for statewide officials and justices
    -- Totals, rebuilt after each sync so the pages don't have to re-add a million rows.
    total_raised    numeric(14,2) NOT NULL DEFAULT 0,
    total_spent     numeric(14,2) NOT NULL DEFAULT 0,
    total_itemized  numeric(14,2) NOT NULL DEFAULT 0,
    n_contributions int NOT NULL DEFAULT 0,
    n_campaigns     int NOT NULL DEFAULT 0,
    first_year      int,
    last_year       int,
    latest_year     int,
    latest_raised   numeric(14,2),
    in_state        numeric(14,2) NOT NULL DEFAULT 0,
    out_of_state    numeric(14,2) NOT NULL DEFAULT 0,
    not_stated      numeric(14,2) NOT NULL DEFAULT 0,
    top_source      text,
    rollup          jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX money_people_list_idx ON money_people (is_current, total_raised DESC);
CREATE INDEX money_people_name_trgm ON money_people USING gin (name gin_trgm_ops);

-- One candidate campaign in one election year, as summarized by the PDC.
CREATE TABLE money_campaigns (
    id               text PRIMARY KEY,   -- PDC summary id, e.g. 'ca-2024-93079'
    person_pk        int NOT NULL REFERENCES money_people (id) ON DELETE CASCADE,
    pdc_person_id    int,
    filer_id         text,
    committee_id     int,
    election_year    int NOT NULL,
    filer_name       text,
    office           text,
    position         text,
    jurisdiction     text,
    district         text,
    party            text,
    reporting_option text,               -- 'mini' filers have no itemized records
    contributions    numeric(14,2),
    carryforward     numeric(14,2),
    expenditures     numeric(14,2),
    loans            numeric(14,2),
    pledges          numeric(14,2),
    debts            numeric(14,2),
    ie_for           numeric(14,2),
    ie_against       numeric(14,2),
    primary_status   text,
    general_status   text,
    url              text,
    updated_at       timestamptz
);
CREATE INDEX money_campaigns_person_idx ON money_campaigns (person_pk, election_year DESC);
CREATE INDEX money_campaigns_committee_idx ON money_campaigns (committee_id);

-- Every itemized contribution to those campaigns.
CREATE TABLE money_contributions (
    id              bigint PRIMARY KEY,
    person_pk       int NOT NULL REFERENCES money_people (id) ON DELETE CASCADE,
    committee_id    int NOT NULL,
    election_year   int,
    report_number   text,
    origin          text,
    amount          numeric(14,2) NOT NULL,
    cash_or_in_kind text,
    receipt_date    date,
    primary_general text,
    source_type     text NOT NULL,
    is_org          boolean NOT NULL,
    contributor_name text NOT NULL,
    contributor_key text NOT NULL,
    city            text,
    state           text,
    zip             text,
    occupation      text,
    employer        text,
    employer_city   text,
    employer_state  text,
    description     text,
    memo            text,
    url             text
);
CREATE INDEX money_contrib_person_date_idx ON money_contributions (person_pk, receipt_date DESC NULLS LAST, id DESC);
CREATE INDEX money_contrib_person_amount_idx ON money_contributions (person_pk, amount DESC);
CREATE INDEX money_contrib_committee_idx ON money_contributions (committee_id, election_year);
CREATE INDEX money_contrib_org_idx ON money_contributions (contributor_key, receipt_date DESC) WHERE is_org;

-- Every reported expenditure by those campaigns.
CREATE TABLE money_expenditures (
    id              bigint PRIMARY KEY,
    person_pk       int NOT NULL REFERENCES money_people (id) ON DELETE CASCADE,
    committee_id    int NOT NULL,
    election_year   int,
    report_number   text,
    origin          text,
    amount          numeric(14,2) NOT NULL,
    itemized        text,
    expenditure_date date,
    category        text NOT NULL,
    description     text,
    recipient_name  text,
    recipient_city  text,
    recipient_state text,
    recipient_zip   text,
    url             text
);
CREATE INDEX money_expend_person_date_idx ON money_expenditures (person_pk, expenditure_date DESC NULLS LAST, id DESC);
CREATE INDEX money_expend_committee_idx ON money_expenditures (committee_id, election_year);

CREATE TABLE money_loans (
    id              bigint PRIMARY KEY,
    person_pk       int NOT NULL REFERENCES money_people (id) ON DELETE CASCADE,
    committee_id    int NOT NULL,
    election_year   int,
    report_number   text,
    origin          text,
    amount          numeric(14,2) NOT NULL,
    endorser_liable numeric(14,2),
    cash_or_in_kind text,
    receipt_date    date,
    loan_due_date   date,
    repayment_schedule text,
    lender_or_endorser text,
    transaction_type text,
    primary_general text,
    lender_name     text,
    lender_city     text,
    lender_state    text,
    lender_occupation text,
    lender_employer text,
    carry_forward   text,
    description     text,
    url             text
);
CREATE INDEX money_loans_person_idx ON money_loans (person_pk, receipt_date DESC NULLS LAST);
CREATE INDEX money_loans_committee_idx ON money_loans (committee_id, election_year);

CREATE TABLE money_debts (
    id              bigint PRIMARY KEY,
    person_pk       int NOT NULL REFERENCES money_people (id) ON DELETE CASCADE,
    committee_id    int NOT NULL,
    election_year   int,
    report_number   text,
    origin          text,
    amount          numeric(14,2) NOT NULL,
    record_type     text,
    from_date       date,
    thru_date       date,
    debt_date       date,
    code            text,
    description     text,
    vendor_name     text,
    vendor_city     text,
    vendor_state    text,
    url             text
);
CREATE INDEX money_debts_person_idx ON money_debts (person_pk, debt_date DESC NULLS LAST);
CREATE INDEX money_debts_committee_idx ON money_debts (committee_id, election_year);

CREATE TABLE money_pledges (
    id              bigint PRIMARY KEY,
    person_pk       int NOT NULL REFERENCES money_people (id) ON DELETE CASCADE,
    committee_id    int NOT NULL,
    election_year   int,
    report_number   text,
    origin          text,
    amount          numeric(14,2) NOT NULL,
    receipt_date    date,
    primary_general text,
    code            text,
    contributor_name text,
    city            text,
    state           text,
    occupation      text,
    employer        text,
    url             text
);
CREATE INDEX money_pledges_person_idx ON money_pledges (person_pk, receipt_date DESC NULLS LAST);
CREATE INDEX money_pledges_committee_idx ON money_pledges (committee_id, election_year);

-- Outside money: independent ads and electioneering that name a candidate (PDC form C-6).
CREATE TABLE money_outside (
    id              text PRIMARY KEY,
    person_pk       int NOT NULL REFERENCES money_people (id) ON DELETE CASCADE,
    election_year   int,
    report_number   text,
    sponsor_id      text,
    sponsor_name    text,
    report_type     text,
    report_date     date,
    for_or_against  text,
    amount          numeric(14,2) NOT NULL DEFAULT 0,
    url             text
);
CREATE INDEX money_outside_person_idx ON money_outside (person_pk, election_year DESC);

-- Who paid for those ads, as declared on the same reports.
CREATE TABLE money_outside_funders (
    id              text PRIMARY KEY,
    report_number   text NOT NULL,
    sponsor_id      text,
    sponsor_name    text,
    funder_name     text,
    city            text,
    state           text,
    occupation      text,
    employer        text,
    amount          numeric(14,2) NOT NULL DEFAULT 0,
    date_received   date,
    url             text
);
CREATE INDEX money_outside_funders_report_idx ON money_outside_funders (report_number);

-- Surplus funds accounts: leftover campaign money, and where it went next.
CREATE TABLE money_surplus (
    id              text PRIMARY KEY,
    person_pk       int NOT NULL REFERENCES money_people (id) ON DELETE CASCADE,
    filer_name      text,
    from_date       date,
    thru_date       date,
    year            int,
    raised          numeric(14,2),
    spent           numeric(14,2),
    balance         numeric(14,2),
    url             text
);
CREATE INDEX money_surplus_person_idx ON money_surplus (person_pk);

CREATE TABLE money_surplus_spending (
    id              bigint PRIMARY KEY,
    person_pk       int NOT NULL REFERENCES money_people (id) ON DELETE CASCADE,
    election_year   int,
    report_number   text,
    amount          numeric(14,2) NOT NULL,
    itemized        text,
    expenditure_date date,
    description     text,
    recipient_name  text,
    recipient_city  text,
    recipient_state text,
    url             text
);
CREATE INDEX money_surplus_spending_person_idx ON money_surplus_spending (person_pk, expenditure_date DESC NULLS LAST);

-- Personal financial affairs statements. `details` holds only the fields on the whitelist in
-- wapeople.money.disclosure: never home addresses or the names of spouses and dependents.
CREATE TABLE money_disclosures (
    id              bigint PRIMARY KEY,
    person_pk       int NOT NULL REFERENCES money_people (id) ON DELETE CASCADE,
    receipt_date    date,
    period_start    date,
    period_end      date,
    url             text,
    details         jsonb
);
CREATE INDEX money_disclosures_person_idx ON money_disclosures (person_pk, period_end DESC NULLS LAST);

-- Organizations that gave to these officials, rebuilt from money_contributions.
CREATE TABLE money_orgs (
    key             text PRIMARY KEY,
    name            text NOT NULL,
    source_type     text,
    total           numeric(14,2) NOT NULL,
    n_gifts         int NOT NULL,
    n_people        int NOT NULL,
    last_date       date
);
CREATE INDEX money_orgs_total_idx ON money_orgs (total DESC);
CREATE INDEX money_orgs_name_trgm ON money_orgs USING gin (name gin_trgm_ops);

-- The next step up the trail: when a donor is itself a registered committee, who funds it.
-- Matched only on an exact normalized name; individuals are reported as a total, never by name.
CREATE TABLE money_committee_sources (
    key              text PRIMARY KEY,
    committee_name   text,
    filer_ids        text[] NOT NULL DEFAULT '{}',
    total_received   numeric(14,2),
    from_individuals numeric(14,2),
    individual_gifts int,
    funders          jsonb NOT NULL DEFAULT '[]'::jsonb,
    synced_at        timestamptz NOT NULL DEFAULT now()
);
