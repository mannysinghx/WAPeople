#!/bin/sh
# Dump the WA People database to backups/wapeople-YYYYmmdd-HHMM.sql.gz
# Restore: gunzip -c backups/<file>.sql.gz | docker compose exec -T db psql -U wapeople wapeople
set -eu
cd "$(dirname "$0")/.."
mkdir -p backups
out="backups/wapeople-$(date +%Y%m%d-%H%M).sql.gz"
docker compose exec -T db pg_dump -U wapeople --no-owner wapeople | gzip > "$out"
echo "wrote $out ($(du -h "$out" | cut -f1))"
