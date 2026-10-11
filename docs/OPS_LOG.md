# Ops log

One line per action with a side effect outside the working tree.
Format: timestamp · what · why · how to undo · verified?

- 2026-10-11 09:38 PDT · Created Docker volume `wapeople_pgdata`, network `wapeople_default`, started container `wapeople-db-1` (postgres:17-alpine) on 127.0.0.1:55440 · persistent DB for the site · `docker compose down` (keeps data); `docker volume rm wapeople_pgdata` deletes data · verified: container healthy
- 2026-10-11 09:50 PDT · Loaded 2025-26 legislature data + started CourtListener case collection and 1991-2024 backfill into local DB (wapeople_pgdata) · initial data load · `TRUNCATE bills, members, court_cases ... CASCADE` or drop volume · verified: 2025-26 counts cross-checked vs session-law chapters
