#!/bin/sh
# Open psql against the local WA People database (reads the password from .env).
cd "$(dirname "$0")/.." || exit 1
PGPASSWORD=$(grep '^POSTGRES_PASSWORD=' .env | cut -d= -f2-) exec psql -h 127.0.0.1 -p "${DB_HOST_PORT:-55440}" -U wapeople wapeople "$@"
