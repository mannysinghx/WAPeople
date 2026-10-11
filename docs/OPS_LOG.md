# Ops log

One line per action with a side effect outside the working tree.
Format: timestamp · what · why · how to undo · verified?

- 2026-10-11 09:38 PDT · Created Docker volume `wapeople_pgdata`, network `wapeople_default`, started container `wapeople-db-1` (postgres:17-alpine) on 127.0.0.1:55440 · persistent DB for the site · `docker compose down` (keeps data); `docker volume rm wapeople_pgdata` deletes data · verified: container healthy
- 2026-10-11 09:50 PDT · Loaded 2025-26 legislature data + started CourtListener case collection and 1991-2024 backfill into local DB (wapeople_pgdata) · initial data load · `TRUNCATE bills, members, court_cases ... CASCADE` or drop volume · verified: 2025-26 counts cross-checked vs session-law chapters
- 2026-10-11 10:41 PDT · Stopped my one-off container `wapeople-backfill-old` (1991-2000 vote refetch) mid-run; marked its sync_runs row as error · it used superseded logic · n/a (full re-import follows) · verified: no other job touched
- 2026-10-11 10:41 PDT · Applied migration 002_vote_kind (roll_calls.vote_kind, history_line) + full re-import of sponsors/votes for all 18 bienniums · classify votes, dedupe, always fetch roll calls · re-run `sync legislature --all --force-details` with previous commit · verifying after run
- 2026-10-11 11:14 PDT · Stopped my dev container `wapeople-web-dev`; started compose service `wapeople-web-1` on 127.0.0.1:8095 (built image) · switch to permanent service · `docker compose stop web` · verified: pages return 200
- 2026-10-11 11:35 PDT · Stopped my one-off container `wapeople-courts-initial`; marked sync_runs row error · CourtListener anonymous daily cap hit (Retry-After 22h); nothing had been written · n/a · verified: court_cases still empty
- 2026-10-11 11:38 PDT · Created then dropped database `wapeople_test` on the project Postgres · integration test for resumable court sync · n/a (dropped) · verified: dropped
- 2026-10-11 11:38 PDT · `docker compose up -d --build`: rebuilt images, restarted `wapeople-web-1`, started `wapeople-sync-1` (scheduler; applies migration 003) · go live locally with daily refresh + resumable court backfill · `docker compose stop sync` / `docker compose down` (keeps data) · verifying
- 2026-10-11 11:45 PDT · Rebuilt + restarted `wapeople-web-1` and `wapeople-sync-1` with final fixes · stage-churn fix, home vote panel · `docker compose up -d` on previous commit · verified: all pages 200, refresh stages_changed=0, court pause until 2026-10-12 09:20 recorded
- 2026-10-11 11:56 PDT · Backup backups/wapeople-20261011-1156.sql.gz (30 MB) before migration 004 · safety net · restore per scripts/backup.sh header · verified: row counts per table checked in dump
- 2026-10-11 11:57 PDT · Applied migration 004_bill_summaries (new summary columns; rebuilt derived bills.search incl. summary) + started summaries load for all bienniums · user asked for bill summaries · restore backups/wapeople-20261011-1156.sql.gz · verifying
- 2026-10-11 12:26 PDT · Loaded staff summaries for all 18 bienniums (74,704 bills, 95.1%; all enacted laws) + rebuilt/restarted web & sync · bill summaries feature · restore backups/wapeople-20261011-1156.sql.gz or NULL the summary columns · verified: 0 junk/bad-char summaries, spot checks across eras, pages 200
