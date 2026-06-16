# MMFileTransfer — Installation Without Docker (Air-Gapped Linux Server)

This guide installs MMFileTransfer directly on a Linux server **without Docker**.
The server has **no internet**, so everything is carried in from an
internet-connected machine and installed offline.

The app is not a single program — it is **6 background services** that all run
at once and talk to each other over `localhost`:

| Service | Role | Runs as |
|---|---|---|
| PostgreSQL | database (users, file metadata, audit) | OS service |
| Redis | Celery broker + Django cache | OS service |
| MinIO | file storage (files live in a folder on this disk) | systemd service |
| Gunicorn | the Django web app | `mmftp-web.service` |
| Celery worker | background jobs (email, file activation, cleanup) | `mmftp-worker.service` |
| Celery beat | scheduler (daily expiry/retention) | `mmftp-beat.service` |
| nginx | reverse proxy + serves downloads | OS service |

> **MinIO stores files in a plain local folder on this server.** It is not a
> cloud service and needs no internet. Keep it — it saves a large code change.

Assumed paths (change if you like, but keep them consistent):

```
/opt/mmftp/app          ← the application code
/opt/mmftp/venv         ← the Python virtual environment
/opt/mmftp/minio-data   ← uploaded files live here
/opt/mmftp/.env         ← configuration (secrets — chmod 600)
```

---

## PART A — On an internet-connected machine (prepare the bundle)

Do this on a machine running the **same OS and CPU architecture** as the server
(e.g. Ubuntu 24.04 x86_64). Compiled Python packages must match the server
exactly, so a throwaway VM of the server's exact OS is the safest choice.

### A1. Python packages (wheelhouse)

```bash
cd /path/to/mmftp
python3.12 -m pip download -r requirements.txt -d wheelhouse/
```

### A2. Offline OS packages

**Ubuntu / Debian:**
```bash
sudo apt-get install --download-only \
  python3.12 python3.12-venv \
  postgresql redis-server nginx libmagic1
# the .deb files are now in /var/cache/apt/archives/ — copy them out:
mkdir -p debs && cp /var/cache/apt/archives/*.deb debs/
```

**RHEL / Rocky / Alma:**
```bash
sudo dnf download --resolve --downloaddir=rpms \
  python3.12 postgresql-server redis nginx file-libs
```

### A3. MinIO binaries

Download the `minio` server and `mc` client binaries (Linux amd64) onto this
machine. Place them in a `bin/` folder.

### A4. The application code

```bash
git archive --format=tar.gz -o mmftp-app.tar.gz HEAD   # or just zip the repo
```

> Make sure migration files are committed before exporting — the app applies
> committed migrations on the server; nothing is generated there.

### A5. Bundle everything

Copy these onto USB / your transfer medium:

```
wheelhouse/        (Python packages)
debs/  or  rpms/   (OS packages)
bin/               (minio, mc)
mmftp-app.tar.gz   (the code)
```

---

## PART B — On the air-gapped server (one-time install)

Run as a user with `sudo`.

### B1. Install OS packages

**Ubuntu / Debian:**
```bash
sudo dpkg -i debs/*.deb
sudo apt-get install -f       # only if dpkg reports missing deps (uses the same debs)
```

**RHEL / Rocky / Alma:**
```bash
sudo dnf install rpms/*.rpm
```

Enable the OS services:
```bash
sudo systemctl enable --now postgresql redis nginx
# RHEL Redis service may be named "redis"; Ubuntu uses "redis-server"
```

### B2. Lay out the app

```bash
sudo mkdir -p /opt/mmftp/{app,minio-data}
sudo tar -xzf mmftp-app.tar.gz -C /opt/mmftp/app
sudo chown -R $USER:$USER /opt/mmftp
```

### B3. Python virtual environment + install from the wheelhouse

```bash
python3.12 -m venv /opt/mmftp/venv
/opt/mmftp/venv/bin/pip install --no-index --find-links /path/to/wheelhouse \
    -r /opt/mmftp/app/requirements.txt
```

### B4. PostgreSQL — create the database and user

```bash
sudo -u postgres psql <<'SQL'
CREATE USER mmftp WITH PASSWORD 'CHANGE_ME_DB_PASSWORD';
CREATE DATABASE mmftp OWNER mmftp;
SQL
```

(If you use a non-default password, put the same one in `.env` at B7.)

### B5. MinIO — install the binary and run it as a service

```bash
sudo install -m 755 bin/minio /usr/local/bin/minio
sudo install -m 755 bin/mc    /usr/local/bin/mc
```

Create `/etc/systemd/system/minio.service`:

```ini
[Unit]
Description=MinIO object storage
After=network.target

[Service]
User=root
Environment=MINIO_ROOT_USER=CHANGE_ME_MINIO_ACCESS_KEY
Environment=MINIO_ROOT_PASSWORD=CHANGE_ME_MINIO_SECRET_KEY
ExecStart=/usr/local/bin/minio server /opt/mmftp/minio-data --address 127.0.0.1:9000 --console-address 127.0.0.1:9001
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Start it and create the bucket:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now minio

mc alias set local http://127.0.0.1:9000 CHANGE_ME_MINIO_ACCESS_KEY CHANGE_ME_MINIO_SECRET_KEY
mc mb --ignore-existing local/mmftp-files
```

### B6. Generate secrets

```bash
# Django secret key:
/opt/mmftp/venv/bin/python -c "import secrets;print(secrets.token_urlsafe(64))"
# Fernet key for encrypting SMTP/LDAP secrets at rest (required in prod):
/opt/mmftp/venv/bin/python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
```

### B7. Configuration file `/opt/mmftp/.env`

Create it (then `chmod 600 /opt/mmftp/.env`). **The key difference from the
Docker setup: hosts are `127.0.0.1`, not container names.**

```ini
# --- Django ---
DJANGO_SECRET_KEY=PASTE_FROM_B6
DJANGO_DEBUG=False
DJANGO_TIME_ZONE=Asia/Kolkata
DJANGO_LOG_LEVEL=INFO
DJANGO_SKIP_MAKEMIGRATIONS=1

# Put the server's real IP/hostname here (what users type in the browser):
DJANGO_ALLOWED_HOSTS=CHANGE_ME_SERVER_IP,localhost,127.0.0.1,[::1]
CSRF_TRUSTED_ORIGINS=http://CHANGE_ME_SERVER_IP

SECRETS_ENCRYPTION_KEY=PASTE_FROM_B6

# nginx runs on the same host, so the proxy is loopback:
TRUSTED_PROXY_IPS=127.0.0.1,::1
DJANGO_SECURE_SSL=False

# --- PostgreSQL (localhost now, not "postgres") ---
POSTGRES_DB=mmftp
POSTGRES_USER=mmftp
POSTGRES_PASSWORD=CHANGE_ME_DB_PASSWORD
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432

# --- Redis (localhost now, not "redis") ---
REDIS_URL=redis://127.0.0.1:6379/0
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
CELERY_BROKER_URL=redis://127.0.0.1:6379/0
CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/1

# --- MinIO (localhost now, not "minio") ---
MINIO_ENDPOINT_URL=http://127.0.0.1:9000
# IMPORTANT: this must be the EXACT URL users type in the browser. nginx proxies
# /mmftp-files/ to MinIO (B9), so this is just the server's base URL.
MINIO_PUBLIC_ENDPOINT_URL=http://CHANGE_ME_SERVER_IP
MINIO_BUCKET=mmftp-files
MINIO_ACCESS_KEY=CHANGE_ME_MINIO_ACCESS_KEY
MINIO_SECRET_KEY=CHANGE_ME_MINIO_SECRET_KEY
MINIO_USE_SSL=False
MINIO_REGION=us-east-1

# --- Email (no SMTP relay in an airgap → log to console) ---
DJANGO_EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend
DEFAULT_FROM_EMAIL=mmftp@mm.co.in
```

> ⚠️ **`MINIO_PUBLIC_ENDPOINT_URL` must be the exact address users hit** (the
> server's IP). Download links are cryptographically signed against this host —
> a mismatch makes every download fail with 403.

### B8. Initialise Django (migrate, static, admin user)

The `.env` is read automatically. Run from the app directory:

```bash
cd /opt/mmftp/app
set -a; source /opt/mmftp/.env; set +a   # load env into this shell

/opt/mmftp/venv/bin/python manage.py migrate
/opt/mmftp/venv/bin/python manage.py collectstatic --noinput
/opt/mmftp/venv/bin/python manage.py createsuperuser
```

### B9. nginx reverse proxy

Create `/etc/nginx/sites-available/mmftp` (Ubuntu) — or
`/etc/nginx/conf.d/mmftp.conf` (RHEL):

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name _;                 # or the server IP/hostname

    client_max_body_size 64m;      # allow large uploads
    proxy_read_timeout    3600s;
    proxy_send_timeout    3600s;

    # The Django app (Gunicorn).
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_request_buffering off;
    }

    # File downloads/previews — presigned links point here, proxy to MinIO.
    # If you change MINIO_BUCKET, change "mmftp-files" below to match.
    location /mmftp-files/ {
        proxy_pass http://127.0.0.1:9000;
        proxy_set_header Host $host;
    }
}
```

Enable + reload:
```bash
# Ubuntu:
sudo ln -sf /etc/nginx/sites-available/mmftp /etc/nginx/sites-enabled/mmftp
sudo rm -f /etc/nginx/sites-enabled/default
# All:
sudo nginx -t && sudo systemctl reload nginx
```

### B10. The three app services (systemd)

These replace the three Docker app containers. WhiteNoise serves static files,
so Gunicorn alone is enough behind nginx.

`/etc/systemd/system/mmftp-web.service`:
```ini
[Unit]
Description=MMFileTransfer web (Gunicorn)
After=network.target postgresql.service redis.service minio.service

[Service]
User=www-data
WorkingDirectory=/opt/mmftp/app
EnvironmentFile=/opt/mmftp/.env
ExecStart=/opt/mmftp/venv/bin/gunicorn mmftp.wsgi:application \
          --bind 127.0.0.1:8000 --workers 3 --timeout 600
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/mmftp-worker.service`:
```ini
[Unit]
Description=MMFileTransfer Celery worker
After=network.target postgresql.service redis.service minio.service

[Service]
User=www-data
WorkingDirectory=/opt/mmftp/app
EnvironmentFile=/opt/mmftp/.env
ExecStart=/opt/mmftp/venv/bin/celery -A mmftp worker -l info
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/mmftp-beat.service`:
```ini
[Unit]
Description=MMFileTransfer Celery beat (scheduler)
After=network.target postgresql.service redis.service

[Service]
User=www-data
WorkingDirectory=/opt/mmftp/app
EnvironmentFile=/opt/mmftp/.env
ExecStart=/opt/mmftp/venv/bin/celery -A mmftp beat -l info
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

> `www-data` must be able to read `/opt/mmftp`. Run:
> `sudo chown -R www-data:www-data /opt/mmftp` (after B8), or use your own
> service user consistently in all three units.

Start them all:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mmftp-web mmftp-worker mmftp-beat
```

---

## PART C — Verify

```bash
# All services running?
systemctl status postgresql redis nginx minio mmftp-web mmftp-worker mmftp-beat

# Health probe (DB + Redis):
curl -s http://127.0.0.1:8000/healthz

# App logs if something is wrong:
journalctl -u mmftp-web -n 80 --no-pager
journalctl -u mmftp-worker -n 80 --no-pager
```

Then open `http://<server-ip>/` in a browser, log in with the admin account from
B8, and do a **test upload and download** to confirm MinIO links resolve.

---

## PART D — Day-to-day operations

```bash
# Restart after a code change (worker/beat have NO autoreload — always restart):
sudo systemctl restart mmftp-web mmftp-worker mmftp-beat

# Tail logs:
journalctl -u mmftp-web -f

# Backup the database:
sudo -u postgres pg_dump mmftp > mmftp-db-$(date +%F).sql

# Backup the files:
tar -czf mmftp-files-$(date +%F).tar.gz -C /opt/mmftp/minio-data .
```

**Applying a code update** (new code carried in):
```bash
sudo tar -xzf mmftp-app-new.tar.gz -C /opt/mmftp/app
cd /opt/mmftp/app
set -a; source /opt/mmftp/.env; set +a
/opt/mmftp/venv/bin/python manage.py migrate
/opt/mmftp/venv/bin/python manage.py collectstatic --noinput
sudo systemctl restart mmftp-web mmftp-worker mmftp-beat
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `DisallowedHost` error | Add the server IP/hostname to `DJANGO_ALLOWED_HOSTS` in `.env`, restart web. |
| Login fails with CSRF error | `CSRF_TRUSTED_ORIGINS` must match scheme+host exactly (`http://` vs `https://`). |
| Upload OK but download 403 / `SignatureDoesNotMatch` | `MINIO_PUBLIC_ENDPOINT_URL` host ≠ the host the browser used. They must match exactly. |
| Download 404 at `/mmftp-files/...` | nginx `location /mmftp-files/` doesn't match `MINIO_BUCKET`. Align them, reload nginx. |
| DB auth failure | `.env` `POSTGRES_PASSWORD` must match what you set in B4. |
| Scheduled cleanup/expiry not running | `systemctl status mmftp-beat mmftp-worker` — both must be running. |
| `pip install` fails: "No matching distribution" | The wheelhouse was built on a different OS/Python/arch than the server. Rebuild on a matching machine (Part A). |

---

## Adding HTTPS later

1. Put the internal-CA cert/key on the server (e.g. `/etc/ssl/mmftp/`).
2. Add a `listen 443 ssl;` server block to the nginx config; redirect `:80 → :443`.
3. In `.env`: set `DJANGO_SECURE_SSL=True` and switch
   `MINIO_PUBLIC_ENDPOINT_URL` + `CSRF_TRUSTED_ORIGINS` to `https://...`.
4. `sudo nginx -t && sudo systemctl reload nginx` and
   `sudo systemctl restart mmftp-web mmftp-worker mmftp-beat`.
