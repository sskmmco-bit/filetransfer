#!/usr/bin/env bash
# =============================================================================
# MMFileTransfer — LOCAL WSL TEST INSTALL (no Docker)
# -----------------------------------------------------------------------------
# This mirrors deploy/INSTALL_NO_DOCKER.md exactly, but with throwaway test
# credentials and `localhost` as the address (WSL2 forwards localhost from
# Windows). It is for validating the procedure on your WSL box — NOT for the
# real server, and NOT to be committed. Delete it when done.
#
# Run it from a WSL terminal:
#     sudo bash /mnt/c/mm_projects/mmftp/deploy/wsl_test_install.sh
#
# Re-running is safe — every step is idempotent.
# =============================================================================
set -euo pipefail

# ---- test settings (throwaway) ----------------------------------------------
APP_SRC=/mnt/c/mm_projects/mmftp           # the Windows repo (source code)
BASE=/opt/mmftp
APP=$BASE/app
VENV=$BASE/venv
DATA=$BASE/minio-data
ENVF=$BASE/.env

DB_PASS=mmftp_test_db_pass
MINIO_KEY=mmftpadmin
MINIO_SECRET=mmftp_test_minio_pass
ADMIN_USER=admin
ADMIN_EMAIL=admin@mm.co.in
ADMIN_PASS=adminpass123

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

log() { echo -e "\n\033[1;36m==> $*\033[0m"; }

[[ $EUID -eq 0 ]] || { echo "Run with sudo: sudo bash $0"; exit 1; }

# ---- STEP 1: system packages -------------------------------------------------
log "STEP 1  apt packages"
apt-get update -y
apt-get install -y python3.12 python3.12-venv postgresql nginx curl rsync
# libmagic shared lib (name changed on 24.04 due to the t64 transition)
apt-get install -y libmagic1t64 || apt-get install -y libmagic1
systemctl enable --now postgresql

# ---- STEP 2: folders + copy code --------------------------------------------
log "STEP 2  lay out /opt/mmftp and copy the code"
mkdir -p "$APP" "$DATA"
rsync -a --delete \
  --exclude '.git' --exclude '.env' --exclude 'venv' \
  --exclude 'staticfiles' --exclude '__pycache__' --exclude '*.pyc' \
  --exclude 'node_modules' \
  "$APP_SRC"/ "$APP"/

# ---- STEP 3: python venv + deps ---------------------------------------------
log "STEP 3  python venv + pip install"
python3.12 -m venv "$VENV"
"$VENV"/bin/pip install --upgrade pip
"$VENV"/bin/pip install -r "$APP"/requirements.txt

# ---- STEP 4: postgres db + user ---------------------------------------------
log "STEP 4  postgres database + user"
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='mmftp'" | grep -q 1 \
  || sudo -u postgres psql -c "CREATE USER mmftp WITH PASSWORD '$DB_PASS';"
sudo -u postgres psql -c "ALTER USER mmftp WITH PASSWORD '$DB_PASS';"
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='mmftp'" | grep -q 1 \
  || sudo -u postgres psql -c "CREATE DATABASE mmftp OWNER mmftp;"

# ---- STEP 5: MinIO -----------------------------------------------------------
log "STEP 5  MinIO binary + service + bucket"
[[ -x /usr/local/bin/minio ]] || { curl -fL https://dl.min.io/server/minio/release/linux-amd64/minio -o /usr/local/bin/minio; chmod +x /usr/local/bin/minio; }
[[ -x /usr/local/bin/mc    ]] || { curl -fL https://dl.min.io/client/mc/release/linux-amd64/mc       -o /usr/local/bin/mc;    chmod +x /usr/local/bin/mc; }

cat >/etc/systemd/system/minio.service <<EOF
[Unit]
Description=MinIO object storage
After=network.target

[Service]
User=root
Environment=MINIO_ROOT_USER=$MINIO_KEY
Environment=MINIO_ROOT_PASSWORD=$MINIO_SECRET
ExecStart=/usr/local/bin/minio server $DATA --address 127.0.0.1:9000 --console-address 127.0.0.1:9001
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now minio
sleep 5
/usr/local/bin/mc alias set local http://127.0.0.1:9000 "$MINIO_KEY" "$MINIO_SECRET"
/usr/local/bin/mc mb --ignore-existing local/mmftp-files

# ---- STEP 6: .env ------------------------------------------------------------
log "STEP 6  write $ENVF"
SECRET=$("$VENV"/bin/python -c "import secrets;print(secrets.token_urlsafe(64))")
FERNET=$("$VENV"/bin/python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
cat >"$ENVF" <<EOF
DJANGO_SECRET_KEY=$SECRET
DJANGO_DEBUG=False
DJANGO_TIME_ZONE=Asia/Kolkata
DJANGO_LOG_LEVEL=INFO
DJANGO_SKIP_MAKEMIGRATIONS=1
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,[::1]
CSRF_TRUSTED_ORIGINS=http://localhost
SECRETS_ENCRYPTION_KEY=$FERNET
TRUSTED_PROXY_IPS=127.0.0.1,::1
DJANGO_SECURE_SSL=False
POSTGRES_DB=mmftp
POSTGRES_USER=mmftp
POSTGRES_PASSWORD=$DB_PASS
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
MINIO_ENDPOINT_URL=http://127.0.0.1:9000
MINIO_PUBLIC_ENDPOINT_URL=http://localhost
MINIO_BUCKET=mmftp-files
MINIO_ACCESS_KEY=$MINIO_KEY
MINIO_SECRET_KEY=$MINIO_SECRET
MINIO_USE_SSL=False
MINIO_REGION=us-east-1
DJANGO_EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend
DEFAULT_FROM_EMAIL=mmftp@mm.co.in
EOF
chmod 600 "$ENVF"

# ---- STEP 7: django init -----------------------------------------------------
log "STEP 7  migrate + collectstatic + superuser"
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
    print("superuser already exists:", u.username)
PY

# ---- STEP 8: nginx -----------------------------------------------------------
log "STEP 8  nginx reverse proxy"
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

    location /mmftp-files/ {
        proxy_pass http://127.0.0.1:9000;
        proxy_set_header Host $host;
    }
}
EOF
# The heredoc is single-quoted (literal), so substitute the app path afterward.
sed -i "s#APP_PATH#$APP#g" /etc/nginx/sites-available/mmftp
ln -sf /etc/nginx/sites-available/mmftp /etc/nginx/sites-enabled/mmftp
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx
systemctl reload nginx

# ---- STEP 9: app services (web + job timers) --------------------------------
log "STEP 9  systemd services for web + job timers"
chown -R www-data:www-data "$APP" "$VENV"

cat >/etc/systemd/system/mmftp-web.service <<EOF
[Unit]
Description=MMFileTransfer web (Gunicorn)
After=network.target postgresql.service minio.service

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
After=network.target postgresql.service minio.service
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

# ---- STEP 10: report ---------------------------------------------------------
log "STEP 10  status"
systemctl is-active postgresql minio nginx mmftp-web || true
systemctl list-timers 'mmftp-*' --no-pager || true
echo
echo "Health probe:"
curl -s -o /dev/null -w "  via gunicorn (8000): HTTP %{http_code}\n" http://127.0.0.1:8000/healthz || true
curl -s -o /dev/null -w "  via nginx    (80):   HTTP %{http_code}\n" http://localhost/healthz || true
echo
echo -e "\033[1;32mDONE.\033[0m  Open http://localhost/ in your Windows browser."
echo "Login:  $ADMIN_USER / $ADMIN_PASS"
