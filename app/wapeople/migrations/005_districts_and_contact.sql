-- Legislative district boundaries (loaded from wapeople/data by wapeople.districts).
CREATE TABLE legislative_districts (
    district   int  PRIMARY KEY,
    plan       text NOT NULL,          -- e.g. 'Washington State Legislative Districts 2024 (Remedial Map 3B)'
    geometry   jsonb NOT NULL,         -- GeoJSON geometry (WGS84)
    label_lon  double precision NOT NULL,
    label_lat  double precision NOT NULL,
    source     jsonb NOT NULL,         -- provenance (publisher, URL, processing, verification)
    loaded_at  timestamptz NOT NULL DEFAULT now()
);

-- Messages sent through the site's contact form. The recipient address lives only in
-- server configuration (CONTACT_TO), never in the code or on the site.
CREATE TABLE contact_messages (
    id           bigserial PRIMARY KEY,
    created_at   timestamptz NOT NULL DEFAULT now(),
    topic        text NOT NULL,
    page         text,                 -- page or bill the message is about, if given
    name         text,
    reply_to     text,                 -- sender's email, only used to reply
    message      text NOT NULL,
    client_hash  text NOT NULL,        -- keyed hash of the client IP, for rate limiting only
    email_status text NOT NULL DEFAULT 'pending',   -- pending | sent | failed
    email_error  text,
    emailed_at   timestamptz,
    attempts     int NOT NULL DEFAULT 0
);
CREATE INDEX contact_messages_client_idx ON contact_messages (client_hash, created_at DESC);
CREATE INDEX contact_messages_status_idx ON contact_messages (email_status, created_at);
