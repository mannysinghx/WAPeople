-- Plain-language summaries from the Legislature's nonpartisan staff documents.
ALTER TABLE bills
    ADD COLUMN summary            text,          -- bullets separated by newlines
    ADD COLUMN summary_source     text,          -- e.g. 'House Bill Analysis 2025'
    ADD COLUMN summary_doc        text,          -- document name of the best candidate checked
    ADD COLUMN summary_url        text,
    ADD COLUMN summary_doc_date   timestamptz,
    ADD COLUMN summary_checked_at timestamptz;

-- Rebuild the search vector so summaries are searchable (the column is derived data).
DROP INDEX bills_search_idx;
ALTER TABLE bills DROP COLUMN search;
ALTER TABLE bills ADD COLUMN search tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('simple', coalesce(bill_id, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(short_description, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(long_description, '')), 'B') ||
    setweight(to_tsvector('english', coalesce(summary, '')), 'B') ||
    setweight(to_tsvector('english', coalesce(legal_title, '')), 'C')
) STORED;
CREATE INDEX bills_search_idx ON bills USING gin (search);
