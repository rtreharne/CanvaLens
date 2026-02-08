#!/usr/bin/env bash
set -euo pipefail

MIGRATE_MAX_ATTEMPTS="${MIGRATE_MAX_ATTEMPTS:-30}"
MIGRATE_RETRY_DELAY="${MIGRATE_RETRY_DELAY:-2}"

if [[ "${SKIP_MIGRATIONS:-0}" != "1" ]]; then
  attempt=1
  until python manage.py migrate --noinput; do
    if [[ "$attempt" -ge "$MIGRATE_MAX_ATTEMPTS" ]]; then
      echo "Migration failed after ${MIGRATE_MAX_ATTEMPTS} attempts."
      exit 1
    fi
    echo "Migration attempt ${attempt}/${MIGRATE_MAX_ATTEMPTS} failed; retrying in ${MIGRATE_RETRY_DELAY}s..."
    attempt=$((attempt + 1))
    sleep "$MIGRATE_RETRY_DELAY"
  done
fi

exec "$@"
