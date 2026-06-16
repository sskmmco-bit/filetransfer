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

# ---- 4. fix ownership + restart the app services ----------------------------
log "Restarting services"
chown -R www-data:www-data "$APP" "$VENV"
systemctl restart mmftp-web mmftp-worker mmftp-beat
sleep 4

log "STATUS"
systemctl is-active mmftp-web mmftp-worker mmftp-beat || true
curl -s -o /dev/null -w "  health: HTTP %{http_code}\n" "http://127.0.0.1/healthz" || true
echo -e "\n\033[1;32mUpdate complete.\033[0m"
