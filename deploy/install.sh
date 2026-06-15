#!/usr/bin/env bash
# MMFileTransfer — air-gapped installer.
#
# Usage (from /opt/mmftp after extracting the deploy bundle):
#   sudo bash deploy/install.sh
#
# First run: loads images, creates .env.production from the template, then stops
# and asks you to fill in the CHANGE_ME_ values. Re-run after editing to start
# the stack and install the host nginx config.

set -euo pipefail

INSTALL_DIR="/opt/mmftp"
IMAGES_TAR="${INSTALL_DIR}/mmftp-images.tar"
COMPOSE_FILE="${INSTALL_DIR}/docker-compose.airgap.yml"
ENV_TEMPLATE="${INSTALL_DIR}/.env.production.template"
ENV_FILE="${INSTALL_DIR}/.env.production"
NGINX_SITE="${INSTALL_DIR}/deploy/nginx.host.conf"

log() { echo -e "\n\033[1;36m==>\033[0m $*"; }
die() { echo -e "\n\033[1;31mERROR:\033[0m $*" >&2; exit 1; }

dc() { docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"; }

# ---- 1. preflight ----
log "Preflight checks"
[[ "$(id -u)" -eq 0 ]] || die "Run with sudo (need root for /opt and nginx)."
command -v docker >/dev/null  || die "docker not installed"
docker compose version >/dev/null 2>&1 || die "docker compose plugin not installed"

[[ -f "$IMAGES_TAR"   ]] || die "Missing $IMAGES_TAR"
[[ -f "$COMPOSE_FILE" ]] || die "Missing $COMPOSE_FILE (did you extract the deploy bundle into ${INSTALL_DIR}?)"
[[ -f "$ENV_TEMPLATE" ]] || die "Missing $ENV_TEMPLATE"

# ---- 2. load images ----
log "Loading Docker images from $IMAGES_TAR"
docker load -i "$IMAGES_TAR"

# ---- 3. env file ----
if [[ ! -f "$ENV_FILE" ]]; then
  log "Creating $ENV_FILE from template"
  cp "$ENV_TEMPLATE" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  if [[ -n "${SUDO_USER:-}" ]] && [[ "$SUDO_USER" != "root" ]]; then
    chown "$SUDO_USER:$SUDO_USER" "$ENV_FILE"
  fi
  echo "    Edit $ENV_FILE and replace every CHANGE_ME_* value, then re-run:"
  echo "        sudo bash deploy/install.sh"
  exit 0
fi

if grep -q "CHANGE_ME_" "$ENV_FILE"; then
  die "$ENV_FILE still contains CHANGE_ME_ placeholders. Edit it first."
fi

# ---- 4. start the stack ----
log "Starting containers"
cd "$INSTALL_DIR"
dc up -d

log "Waiting 15s for the web container to migrate and come up..."
sleep 15
dc ps

# The web entrypoint applies migrations, collects static, and bootstraps the
# superuser from DJANGO_SUPERUSER_*; the createbuckets one-shot makes the MinIO
# bucket. No separate seed/migrate step is required here.

# ---- 5. host nginx ----
if command -v nginx >/dev/null; then
  log "Installing host nginx config"
  install -m 644 "$NGINX_SITE" /etc/nginx/sites-available/mmftp
  ln -sf /etc/nginx/sites-available/mmftp /etc/nginx/sites-enabled/mmftp
  [[ -L /etc/nginx/sites-enabled/default ]] && rm -f /etc/nginx/sites-enabled/default
  nginx -t

  if command -v systemctl >/dev/null && systemctl --quiet is-system-running 2>/dev/null; then
    if systemctl --quiet is-active nginx; then
      systemctl reload nginx
    else
      systemctl enable --now nginx
    fi
  else
    if service nginx status >/dev/null 2>&1; then
      service nginx reload
    else
      service nginx start
    fi
  fi
else
  echo "    nginx not installed on host. Skipping."
  echo "    The app is reachable only on 127.0.0.1:8080 until a host proxy is set up."
  echo "    Install nginx and re-run, or expose the nginx container's port directly."
fi

log "Done"
echo "Stack status:"
dc ps
echo ""
echo "App should be reachable at http://<server-ip>/ — log in with the"
echo "DJANGO_SUPERUSER_USERNAME / DJANGO_SUPERUSER_PASSWORD from .env.production."
