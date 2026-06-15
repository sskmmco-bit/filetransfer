#!/usr/bin/env bash
set -euo pipefail

POSTGRES_HOST="${POSTGRES_HOST:-postgres}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"

echo "[entrypoint] waiting for postgres at ${POSTGRES_HOST}:${POSTGRES_PORT} ..."
until nc -z "${POSTGRES_HOST}" "${POSTGRES_PORT}"; do
  sleep 1
done
echo "[entrypoint] postgres is up."

# In production, migrations are committed to the image — skip generation and
# only apply them. Set DJANGO_SKIP_MAKEMIGRATIONS=1 (the prod env file does) to
# avoid generating uncommitted schema changes at boot. Dev leaves it unset.
if [ "${DJANGO_SKIP_MAKEMIGRATIONS:-0}" != "1" ]; then
  echo "[entrypoint] generating migrations (idempotent) ..."
  python manage.py makemigrations accounts config files notifications audit public core --noinput
fi

echo "[entrypoint] applying migrations ..."
python manage.py migrate --noinput

# Collect static (safe to run every boot; required behind nginx/gunicorn).
if [ "${DJANGO_COLLECTSTATIC:-1}" = "1" ]; then
  echo "[entrypoint] collecting static ..."
  python manage.py collectstatic --noinput
fi

# Optionally bootstrap a superuser from env (idempotent).
if [ -n "${DJANGO_SUPERUSER_USERNAME:-}" ] && [ -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]; then
  echo "[entrypoint] ensuring superuser '${DJANGO_SUPERUSER_USERNAME}' exists ..."
  python manage.py shell <<'PY'
import os
from django.contrib.auth import get_user_model
from apps.accounts.models import Role, RoleSlug
U = get_user_model()
username = os.environ["DJANGO_SUPERUSER_USERNAME"]
email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "")
password = os.environ["DJANGO_SUPERUSER_PASSWORD"]
u, created = U.objects.get_or_create(username=username, defaults={"email": email})
if created:
    u.email = email
    u.is_staff = True
    u.is_superuser = True
    u.role = Role.objects.filter(slug=RoleSlug.SUPERADMIN).first()
    u.set_password(password)
    u.save()
    print("  created superuser", username)
else:
    print("  superuser already exists:", username)
PY
fi

echo "[entrypoint] starting: $*"
exec "$@"
