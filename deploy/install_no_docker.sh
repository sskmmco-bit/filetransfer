#!/usr/bin/env bash
# =============================================================================
# MMFileTransfer — one-command installer (no Docker, Ubuntu/Debian)
# -----------------------------------------------------------------------------
# Installs and starts the whole stack (PostgreSQL, Gunicorn, nginx, and the
# background-job systemd timers) on a clean Linux server. File blobs are stored
# on local disk (no MinIO/S3). Needs internet ONLY while running; the app runs
# fully offline afterward.
#
# USAGE — copy the repo to the server, then run ONE command from the repo root:
#
#     sudo bash deploy/install_no_docker.sh
#
# It auto-detects the server's IP. To force a specific IP/hostname:
#
#     sudo SERVER_IP=10.20.30.40 bash deploy/install_no_docker.sh
#
# Strong passwords + secrets are generated automatically and saved (root-only)
# to /opt/mmftp/.install-credentials. The admin login is printed at the end.
# Re-running is safe (idempotent).
#
# Targets Ubuntu/Debian (apt). For RHEL/Rocky, ask the dev team for the dnf
# variant (Postgres pg_hba + SELinux differ).
# =============================================================================
set -euo pipefail

BASE=/opt/mmftp
APP=$BASE/app
VENV=$BASE/venv
FILES=$BASE/files          # local file-blob store (FILE_STORAGE_ROOT)
ENVF=$BASE/.env
CREDS=$BASE/.install-credentials

ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@mm.co.in}"

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

log()  { echo -e "\n\033[1;36m==> $*\033[0m"; }
die()  { echo -e "\n\033[1;31mERROR: $*\033[0m" >&2; exit 1; }
gen()  { tr -dc 'A-Za-z0-9' </dev/urandom | head -c "${1:-24}"; }

[[ $EUID -eq 0 ]] || die "Run with sudo:  sudo bash deploy/install_no_docker.sh"
command -v apt-get >/dev/null || die "This installer targets Ubuntu/Debian (apt). Ask for the RHEL/dnf variant."

# Locate the repo (this script lives in <repo>/deploy/).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# Server address users will type in the browser.
SERVER_IP="${SERVER_IP:-$(hostname -I | awk '{print $1}')}"
[[ -n "$SERVER_IP" ]] || die "Could not detect server IP. Re-run with: sudo SERVER_IP=<ip> bash ..."
log "Using SERVER_IP=$SERVER_IP   (override with SERVER_IP=... if wrong)"

# ---- STEP 1: system packages -------------------------------------------------
log "STEP 1  apt packages"
apt-get update -y
apt-get install -y python3.12 python3.12-venv postgresql nginx curl rsync
apt-get install -y libmagic1t64 || apt-get install -y libmagic1
systemctl enable --now postgresql

# ---- STEP 1b: PostgreSQL tuning (4 GB/2 vCPU drop-in) ------------------------
log "STEP 1b  PostgreSQL tuning drop-in"
PG_CONFD="$(find /etc/postgresql -maxdepth 3 -type d -name conf.d 2>/dev/null | head -n1)"
if [[ -n "$PG_CONFD" ]]; then
  install -m 644 "$SCRIPT_DIR/postgresql.tuning.conf" "$PG_CONFD/10-mmftp-tuning.conf"
  systemctl restart postgresql
  log "  applied $PG_CONFD/10-mmftp-tuning.conf and restarted postgresql"
else
  log "  conf.d not found — apply deploy/postgresql.tuning.conf manually"
fi

# ---- STEP 2: swap (safety net on low-RAM servers) ---------------------------
if [[ "$(swapon --show --noheadings | wc -l)" -eq 0 && ! -f /swapfile ]]; then
  log "STEP 2  creating a 4G swap file (no swap detected)"
  fallocate -l 4G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=4096
  chmod 600 /swapfile; mkswap /swapfile; swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >>/etc/fstab
else
  log "STEP 2  swap already present — skipping"
fi

# ---- STEP 3: lay out folders + copy code ------------------------------------
log "STEP 3  install code into $APP"
mkdir -p "$APP" "$FILES"
if [[ "$REPO_ROOT" != "$APP" ]]; then
  rsync -a --delete \
    --exclude '.git' --exclude '.env' --exclude 'venv' \
    --exclude 'staticfiles' --exclude '__pycache__' --exclude '*.pyc' \
    --exclude 'node_modules' --exclude 'media' --exclude 'files' \
    "$REPO_ROOT"/ "$APP"/
fi

# ---- STEP 4: python venv + deps ---------------------------------------------
log "STEP 4  python venv + pip install"
python3.12 -m venv "$VENV"
"$VENV"/bin/pip install --upgrade pip
"$VENV"/bin/pip install -r "$APP"/requirements.txt

# ---- STEP 5: credentials (generated once, reused on re-run) ------------------
log "STEP 5  credentials"
if [[ -f "$CREDS" ]]; then
  # shellcheck disable=SC1090
  . "$CREDS"
  echo "    reusing existing credentials from $CREDS"
else
  DB_PASS="$(gen 24)"
  ADMIN_PASS="$(gen 16)"
  SECRET_KEY="$("$VENV"/bin/python -c 'import secrets;print(secrets.token_urlsafe(64))')"
  FERNET="$("$VENV"/bin/python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')"
  umask 077
  cat >"$CREDS" <<EOF
DB_PASS='$DB_PASS'
ADMIN_PASS='$ADMIN_PASS'
SECRET_KEY='$SECRET_KEY'
FERNET='$FERNET'
EOF
  chmod 600 "$CREDS"
  echo "    generated and saved to $CREDS (root-only)"
fi

# ---- STEP 6: postgres db + user (detect the cluster's real port) ------------
log "STEP 6  postgres database + user"
PG_PORT="$(pg_lsclusters -h 2>/dev/null | awk 'NR==1{print $3}')"; PG_PORT="${PG_PORT:-5432}"
echo "    postgres cluster is on port $PG_PORT"
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='mmftp'" | grep -q 1 \
  || sudo -u postgres psql -c "CREATE USER mmftp WITH PASSWORD '$DB_PASS';"
sudo -u postgres psql -c "ALTER USER mmftp WITH PASSWORD '$DB_PASS';"
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='mmftp'" | grep -q 1 \
  || sudo -u postgres psql -c "CREATE DATABASE mmftp OWNER mmftp;"

# ---- STEP 7: local file-blob store ------------------------------------------
# No MinIO/S3: blobs live on local disk at $FILES (FILE_STORAGE_ROOT). nginx
# serves them via X-Accel-Redirect, so the dir must be readable by nginx
# (www-data) and writable by the app (also www-data here).
log "STEP 7  local file-blob store at $FILES"
mkdir -p "$FILES"
chown -R www-data:www-data "$FILES"
chmod 750 "$FILES"

# ---- STEP 8: .env ------------------------------------------------------------
log "STEP 8  write $ENVF"
umask 077
cat >"$ENVF" <<EOF
DJANGO_SECRET_KEY=$SECRET_KEY
DJANGO_DEBUG=False
DJANGO_TIME_ZONE=Asia/Kolkata
DJANGO_LOG_LEVEL=INFO
DJANGO_SKIP_MAKEMIGRATIONS=1
DJANGO_ALLOWED_HOSTS=$SERVER_IP,localhost,127.0.0.1,[::1]
CSRF_TRUSTED_ORIGINS=http://$SERVER_IP
SECRETS_ENCRYPTION_KEY=$FERNET
TRUSTED_PROXY_IPS=127.0.0.1,::1
DJANGO_SECURE_SSL=False
POSTGRES_DB=mmftp
POSTGRES_USER=mmftp
POSTGRES_PASSWORD=$DB_PASS
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=$PG_PORT
FILE_STORAGE_ROOT=$FILES
FILE_STORAGE_USE_X_ACCEL=True
FILE_STORAGE_X_ACCEL_PREFIX=/_protected/
DJANGO_EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend
DEFAULT_FROM_EMAIL=mmftp@mm.co.in
EOF
chmod 600 "$ENVF"

# ---- STEP 9: migrate + collectstatic + superuser ----------------------------
log "STEP 9  migrate + collectstatic + superuser"
set -a; . "$ENVF"; set +a
cd "$APP"
"$VENV"/bin/python manage.py migrate --noinput
"$VENV"/bin/python manage.py collectstatic --noinput
DJANGO_SUPERUSER_USERNAME="$ADMIN_USER" \
DJANGO_SUPERUSER_EMAIL="$ADMIN_EMAIL" \
DJANGO_SUPERUSER_PASSWORD="$ADMIN_PASS" \
"$VENV"/bin/python manage.py shell <<'PY'
import os
from django.contrib.auth import get_user_model
from apps.accounts.models import Role, RoleSlug
U = get_user_model()
u, created = U.objects.get_or_create(
    username=os.environ["DJANGO_SUPERUSER_USERNAME"],
    defaults={"email": os.environ.get("DJANGO_SUPERUSER_EMAIL", "")},
)
if created:
    u.is_staff = True
    u.is_superuser = True
    u.role = Role.objects.filter(slug=RoleSlug.SUPERADMIN).first()
    u.set_password(os.environ["DJANGO_SUPERUSER_PASSWORD"])
    u.save()
    print("created superuser:", u.username)
else:
    print("superuser already exists (password unchanged):", u.username)
PY

# ---- STEP 10: nginx ----------------------------------------------------------
log "STEP 10  nginx reverse proxy"
cat >/etc/nginx/sites-available/mmftp <<'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name _;

    client_max_body_size 64m;
    proxy_read_timeout    3600s;
    proxy_send_timeout    3600s;

    gzip on;
    gzip_types text/plain text/css application/javascript application/json image/svg+xml;
    gzip_min_length 1024;

    # Static assets are content-hashed (CompressedManifestStaticFilesStorage), so
    # serve them straight from disk with a long immutable cache — off the Python tier.
    location /static/ {
        alias APP_PATH/staticfiles/;
        expires 1y;
        add_header Cache-Control "public, immutable";
        access_log off;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_request_buffering off;
    }

    # Protected blob store: the app authorizes, then returns an X-Accel-Redirect
    # into this internal location so nginx serves the bytes (zero-copy).
    location /_protected/ {
        internal;
        alias FILES_PATH/;
        sendfile on;
        access_log off;
    }
}
EOF
# The heredoc is single-quoted (literal), so substitute the paths afterward.
sed -i "s#APP_PATH#$APP#g; s#FILES_PATH#$FILES#g" /etc/nginx/sites-available/mmftp
ln -sf /etc/nginx/sites-available/mmftp /etc/nginx/sites-enabled/mmftp
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx
systemctl reload nginx

# ---- STEP 11: app services ---------------------------------------------------
log "STEP 11  systemd services (web + job timers)"
chown -R www-data:www-data "$APP" "$VENV"

cat >/etc/systemd/system/mmftp-web.service <<EOF
[Unit]
Description=MMFileTransfer web (Gunicorn)
After=network.target postgresql.service

[Service]
User=www-data
WorkingDirectory=$APP
EnvironmentFile=$ENVF
ExecStart=$VENV/bin/gunicorn mmftp.wsgi:application --bind 127.0.0.1:8000 --workers 3 --threads 3 --preload --max-requests 1000 --max-requests-jitter 200 --timeout 600
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

# Background jobs run as oneshot management commands on timers (no Celery worker/beat).
# A shared OnFailure helper tags failures into the journal (logger -t mmftp-job).
cat >/etc/systemd/system/mmftp-onfailure@.service <<'EOF'
[Unit]
Description=Log failure of %i

[Service]
Type=oneshot
ExecStart=/usr/bin/logger -t mmftp-job "unit %i failed"
EOF

mk_job() {  # $1=unit-name  $2=description  $3=manage.py command
  cat >/etc/systemd/system/$1.service <<EOF
[Unit]
Description=$2
After=network.target postgresql.service
OnFailure=mmftp-onfailure@%n.service

[Service]
Type=oneshot
User=www-data
WorkingDirectory=$APP
EnvironmentFile=$ENVF
ExecStart=$VENV/bin/python $APP/manage.py $3
EOF
}

mk_job mmftp-notifications "MMFileTransfer deferred-email drain"  "send_queued_notifications"
mk_job mmftp-purge         "MMFileTransfer daily purge/expiry"    "purge_expired_files"
mk_job mmftp-reminders     "MMFileTransfer daily expiry reminders" "send_expiry_reminders"
mk_job mmftp-thumbnails    "MMFileTransfer pending-thumbnail generation" "generate_pending_thumbnails"

cat >/etc/systemd/system/mmftp-notifications.timer <<'EOF'
[Unit]
Description=Drain the deferred-email queue every 2 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=2min
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat >/etc/systemd/system/mmftp-purge.timer <<'EOF'
[Unit]
Description=Daily file purge / expiry cleanup

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat >/etc/systemd/system/mmftp-reminders.timer <<'EOF'
[Unit]
Description=Daily expiry-reminder emails

[Timer]
OnCalendar=*-*-* 07:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat >/etc/systemd/system/mmftp-thumbnails.timer <<'EOF'
[Unit]
Description=Generate pending image thumbnails every 2 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=2min
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now mmftp-web mmftp-notifications.timer mmftp-purge.timer mmftp-reminders.timer mmftp-thumbnails.timer
sleep 6

# ---- done --------------------------------------------------------------------
log "STATUS"
systemctl is-active postgresql nginx mmftp-web || true
systemctl list-timers 'mmftp-*' --no-pager || true
echo
curl -s -o /dev/null -w "  health via nginx (80): HTTP %{http_code}\n" "http://127.0.0.1/healthz" || true
echo
echo -e "\033[1;32m======================================================================\033[0m"
echo -e "\033[1;32m DONE.\033[0m  Open  http://$SERVER_IP/  in a browser."
echo "   Admin login:    $ADMIN_USER / $ADMIN_PASS"
echo "   All secrets saved (root-only) in:  $CREDS"
echo "   Note the admin password now, then you may delete $CREDS."
echo -e "\033[1;32m======================================================================\033[0m"
