#!/usr/bin/env bash
# =============================================================================
# MMFileTransfer — update an existing no-Docker install (Ubuntu/Debian)
# -----------------------------------------------------------------------------
# Run this AFTER the first install (install_no_docker.sh) to deploy new code.
# It does NOT touch your .env, database, files, or credentials — only the code.
#
# Workflow:
#   1. Pull the latest code into your working clone:   git pull
#   2. From that clone's root, run:                     sudo bash deploy/update_no_docker.sh
#
# Keep your git clone in a normal directory (e.g. ~/mmftp), NOT in /opt/mmftp/app
# (that copy is owned by www-data and managed by these scripts).
# =============================================================================
set -euo pipefail

BASE=/opt/mmftp
APP=$BASE/app
VENV=$BASE/venv
ENVF=$BASE/.env

log() { echo -e "\n\033[1;36m==> $*\033[0m"; }
die() { echo -e "\n\033[1;31mERROR: $*\033[0m" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run with sudo:  sudo bash deploy/update_no_docker.sh"
[[ -f "$ENVF" ]] || die "$ENVF not found — run the first-time installer (install_no_docker.sh) first."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# ---- 1. copy the new code (never overwrites .env / venv / data) -------------
log "Updating code in $APP"
if [[ "$REPO_ROOT" != "$APP" ]]; then
  rsync -a --delete \
    --exclude '.git' --exclude '.env' --exclude 'venv' \
    --exclude 'staticfiles' --exclude '__pycache__' --exclude '*.pyc' \
    --exclude 'node_modules' --exclude 'minio-data' \
    "$REPO_ROOT"/ "$APP"/
else
  die "Run this from your git clone (e.g. ~/mmftp), not from $APP."
fi

# ---- 2. update Python deps (in case requirements.txt changed) ---------------
# Needs internet only if new/changed packages. No change = nothing downloaded.
log "Syncing Python dependencies"
"$VENV"/bin/pip install -r "$APP"/requirements.txt

# ---- 3. apply migrations + refresh static -----------------------------------
log "migrate + collectstatic"
set -a; . "$ENVF"; set +a
cd "$APP"
"$VENV"/bin/python manage.py migrate --noinput
"$VENV"/bin/python manage.py collectstatic --noinput

# ---- 4. migrate Celery worker/beat -> job timers (idempotent) ---------------
# Older installs ran mmftp-worker / mmftp-beat (Celery). Retire them and (re)install
# the oneshot services + timers that replaced them. Safe to run every update.
if systemctl list-unit-files 'mmftp-worker.service' 'mmftp-beat.service' \
     --no-legend 2>/dev/null | grep -q .; then
  log "Retiring legacy Celery units (mmftp-worker / mmftp-beat)"
  systemctl disable --now mmftp-worker.service mmftp-beat.service 2>/dev/null || true
  rm -f /etc/systemd/system/mmftp-worker.service /etc/systemd/system/mmftp-beat.service
fi

log "Installing background-job timers"
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

# ---- 4b. refresh web unit, nginx config, and PG tuning (idempotent) ---------
# Re-applied every update so config-tier perf changes (Gunicorn --preload,
# nginx /static/ + gzip, PostgreSQL tuning) reach existing installs.
log "Refreshing web unit + nginx + PostgreSQL tuning"
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
sed -i "s#APP_PATH#$APP#g" /etc/nginx/sites-available/mmftp
ln -sf /etc/nginx/sites-available/mmftp /etc/nginx/sites-enabled/mmftp
nginx -t && systemctl reload nginx || log "nginx config test failed — left previous config running"

PG_CONFD="$(find /etc/postgresql -maxdepth 3 -type d -name conf.d 2>/dev/null | head -n1)"
if [[ -n "$PG_CONFD" ]]; then
  install -m 644 "$SCRIPT_DIR/postgresql.tuning.conf" "$PG_CONFD/10-mmftp-tuning.conf"
  systemctl restart postgresql
  log "  applied $PG_CONFD/10-mmftp-tuning.conf"
fi

# ---- 5. fix ownership + restart/enable services -----------------------------
log "Restarting services"
chown -R www-data:www-data "$APP" "$VENV"
systemctl daemon-reload
systemctl restart mmftp-web
systemctl enable --now mmftp-notifications.timer mmftp-purge.timer mmftp-reminders.timer mmftp-thumbnails.timer
sleep 4

log "STATUS"
systemctl is-active mmftp-web || true
systemctl list-timers 'mmftp-*' --no-pager || true
curl -s -o /dev/null -w "  health: HTTP %{http_code}\n" "http://127.0.0.1/healthz" || true
echo -e "\n\033[1;32mUpdate complete.\033[0m"
