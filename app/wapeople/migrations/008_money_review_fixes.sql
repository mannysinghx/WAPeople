-- Amended financial statements keep their id and get a new submission_id, so storing it lets the
-- sync notice them. Statements loaded before this have none and are fetched once more.
ALTER TABLE money_disclosures ADD COLUMN submission_id text;

-- Stored committee funders may name people coded as organizations. Marking them old makes the next
-- sync rebuild them under the new rule instead of waiting out their seven days.
UPDATE money_committee_sources SET synced_at = now() - interval '30 days';
