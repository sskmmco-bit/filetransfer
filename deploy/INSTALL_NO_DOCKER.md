# MMFileTransfer — Installation Without Docker (Linux Server)

This guide installs MMFileTransfer directly on a Linux server **without Docker**.

> **Internet:** the server needs internet **only during installation** (to
> download the software). Once installed, the app **runs fully offline** — it
> never needs internet again. The only thing that needs the internet is the
> install step below.

> ### ⚡ Fast path — one command
> If you just want it installed, copy the repo to the server and run:
> ```bash
> sudo bash deploy/install_no_docker.sh
> ```
> It auto-detects the server IP, generates strong passwords/secrets, adds swap,
> and starts everything — printing the admin login at the end. (Override the IP
> with `sudo SERVER_IP=10.20.30.40 bash deploy/install_no_docker.sh`.) Targets
> Ubuntu/Debian. The manual steps below explain what that script does, for
> understanding, customization, or RHEL.

The app is not a single program — it is **6 services** that all run at once and
talk to each other over `localhost`:

| Service | Role | Runs as |
|---|---|---|
| PostgreSQL | database (users, file metadata, audit) | OS service |
| MinIO | file storage (files live in a folder on this disk) | systemd service |
| Gunicorn | the Django web app | `mmftp-web.service` |
| Deferred-email drain | sends queued emails (~every 2 min) | `mmftp-notifications.timer` |
| Thumbnail generation | thumbnails new images (~every 2 min) | `mmftp-thumbnails.timer` |
| Daily purge | expiry/retention + abandoned-upload cleanup | `mmftp-purge.timer` |
| Daily reminders | expiry-reminder emails | `mmftp-reminders.timer` |
| nginx | reverse proxy + serves downloads | OS service |

> **MinIO stores files in a plain local folder on this server.** It is not a
> cloud service. The files never leave the box.

### Folder layout

You create one main folder, `/opt/mmftp`, with these inside:

```
/opt/mmftp/
├── app/            ← the application CODE
├── venv/           ← Python + all the packages
├── minio-data/     ← empty at first; MinIO stores uploaded files here
└── .env            ← configuration (holds secrets — chmod 600)
```

A few pieces go to standard system locations:

```
/usr/local/bin/minio , /usr/local/bin/mc      ← the MinIO program + admin tool
/etc/systemd/system/*.service                 ← the service files (auto-start)
/etc/nginx/sites-available/mmftp              ← the nginx config
```

---

## STEP 1 — Install the system software (needs internet)

**Ubuntu / Debian:**
```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv postgresql nginx libmagic1
sudo systemctl enable --now postgresql nginx
```

**RHEL / Rocky / Alma:**
```bash
sudo dnf install -y python3.12 postgresql-server nginx file-libs
sudo postgresql-setup --initdb
sudo systemctl enable --now postgresql nginx
```

## STEP 2 — Put the code on the server

```bash
sudo mkdir -p /opt/mmftp/{app,minio-data}
sudo chown -R $USER:$USER /opt/mmftp

# Copy the code in however you like — clone it, or copy the folder across:
git clone <your-repo-url> /opt/mmftp/app
```

> Make sure the migration files are committed in the code — the app applies
> committed migrations; it does not generate them here.

## STEP 3 — Python environment + packages (needs internet)

```bash
python3.12 -m venv /opt/mmftp/venv
/opt/mmftp/venv/bin/pip install -r /opt/mmftp/app/requirements.txt
```

## STEP 4 — PostgreSQL: create the database and user

```bash
sudo -u postgres psql <<'SQL'
CREATE USER mmftp WITH PASSWORD 'CHOOSE_A_DB_PASSWORD';
CREATE DATABASE mmftp OWNER mmftp;
SQL
```

Use the **same** password in the `.env` (Step 6).

## STEP 5 — MinIO: install the binary and run it as a service (needs internet)

```bash
sudo curl -L https://dl.min.io/server/minio/release/linux-amd64/minio -o /usr/local/bin/minio
sudo curl -L https://dl.min.io/client/mc/release/linux-amd64/mc -o /usr/local/bin/mc
sudo chmod +x /usr/local/bin/minio /usr/local/bin/mc
```

Create `/etc/systemd/system/minio.service` (pick your own access/secret keys —
the secret must be at least 8 characters):

```ini
[Unit]
Description=MinIO object storage
After=network.target

[Service]
User=root
Environment=MINIO_ROOT_USER=CHOOSE_A_MINIO_KEY
Environment=MINIO_ROOT_PASSWORD=CHOOSE_A_MINIO_SECRET
ExecStart=/usr/local/bin/minio server /opt/mmftp/minio-data --address 127.0.0.1:9000 --console-address 127.0.0.1:9001
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Start it and create the bucket (use the same keys you just set):

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now minio

mc alias set local http://127.0.0.1:9000 CHOOSE_A_MINIO_KEY CHOOSE_A_MINIO_SECRET
mc mb --ignore-existing local/mmftp-files
```

## STEP 6 — Configuration: `/opt/mmftp/.env`

First generate the two secrets:

```bash
/opt/mmftp/venv/bin/python -c "import secrets;print(secrets.token_urlsafe(64))"               # DJANGO_SECRET_KEY
/opt/mmftp/venv/bin/python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"   # SECRETS_ENCRYPTION_KEY
```

Create `/opt/mmftp/.env`. Below is a **filled-in example** assuming the server
IP is `192.168.1.50` — replace it with your real IP/hostname:

```ini
# --- Django ---
DJANGO_SECRET_KEY=PASTE_THE_64_CHAR_SECRET_HERE
DJANGO_DEBUG=False
DJANGO_TIME_ZONE=Asia/Kolkata
DJANGO_LOG_LEVEL=INFO
DJANGO_SKIP_MAKEMIGRATIONS=1

DJANGO_ALLOWED_HOSTS=192.168.1.50,localhost,127.0.0.1,[::1]
CSRF_TRUSTED_ORIGINS=http://192.168.1.50

SECRETS_ENCRYPTION_KEY=PASTE_THE_FERNET_KEY_HERE

TRUSTED_PROXY_IPS=127.0.0.1,::1
DJANGO_SECURE_SSL=False

# --- PostgreSQL (same machine → 127.0.0.1) ---
POSTGRES_DB=mmftp
POSTGRES_USER=mmftp
POSTGRES_PASSWORD=CHOOSE_A_DB_PASSWORD       # must match Step 4
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432

# --- Cache: in-process LocMemCache, nothing to configure (no Redis) ---

# --- MinIO ---
MINIO_ENDPOINT_URL=http://127.0.0.1:9000          # how the APP reaches MinIO
MINIO_PUBLIC_ENDPOINT_URL=http://192.168.1.50     # how the BROWSER reaches it (real IP!)
MINIO_BUCKET=mmftp-files
MINIO_ACCESS_KEY=CHOOSE_A_MINIO_KEY               # must match minio.service (Step 5)
MINIO_SECRET_KEY=CHOOSE_A_MINIO_SECRET            # must match minio.service (Step 5)
MINIO_USE_SSL=False
MINIO_REGION=us-east-1

# --- Email (offline → just log emails instead of sending) ---
DJANGO_EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend
DEFAULT_FROM_EMAIL=mmftp@mm.co.in
```

Lock it down (it holds passwords):

```bash
chmod 600 /opt/mmftp/.env
```

### The values you actually decide (everything else stays as-is)

| Value | Where it comes from |
|---|---|
| `DJANGO_SECRET_KEY` | generate (command above) |
| `SECRETS_ENCRYPTION_KEY` | generate (command above) |
| `POSTGRES_PASSWORD` | you pick it — must match Step 4 |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | you pick them — must match `minio.service` (Step 5) |
| The IP `192.168.1.50` | your real server IP — appears in 3 places: `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `MINIO_PUBLIC_ENDPOINT_URL` |

### Three rules that trip people up

1. **`127.0.0.1` vs the real IP.** Services on the same box reach each other via
   `127.0.0.1`. But `MINIO_PUBLIC_ENDPOINT_URL`, `ALLOWED_HOSTS`, and
   `CSRF_TRUSTED_ORIGINS` use the **real server IP**, because that's what a
   person's browser types. Mixing these up is the #1 cause of failures.
2. **Passwords must match in two places** — DB password (`.env` ↔ Step 4), MinIO
   keys (`.env` ↔ `minio.service`).
3. **`MINIO_PUBLIC_ENDPOINT_URL` must be the exact address users hit** — download
   links are signed against it; a mismatch makes downloads fail with 403.

## STEP 7 — Initialise Django (migrate, static, admin user)

```bash
cd /opt/mmftp/app
set -a; source /opt/mmftp/.env; set +a   # load the env into this shell

/opt/mmftp/venv/bin/python manage.py migrate
/opt/mmftp/venv/bin/python manage.py collectstatic --noinput
/opt/mmftp/venv/bin/python manage.py createsuperuser
```

## STEP 8 — nginx reverse proxy

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

## STEP 9 — The web service + background-job timers (systemd)

One always-on web service plus three **oneshot** jobs fired by **systemd timers**
(no Celery worker/broker). WhiteNoise serves static files, so Gunicorn alone is
enough behind nginx.

`/etc/systemd/system/mmftp-web.service`:
```ini
[Unit]
Description=MMFileTransfer web (Gunicorn)
After=network.target postgresql.service minio.service

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

A shared failure logger (tags any failed job into the journal as `mmftp-job`):
```ini
# /etc/systemd/system/mmftp-onfailure@.service
[Unit]
Description=Log failure of %i

[Service]
Type=oneshot
ExecStart=/usr/bin/logger -t mmftp-job "unit %i failed"
```

Each job is a `Type=oneshot` service — e.g. the deferred-email drain
`/etc/systemd/system/mmftp-notifications.service`:
```ini
[Unit]
Description=MMFileTransfer deferred-email drain
After=network.target postgresql.service minio.service
OnFailure=mmftp-onfailure@%n.service

[Service]
Type=oneshot
User=www-data
WorkingDirectory=/opt/mmftp/app
EnvironmentFile=/opt/mmftp/.env
ExecStart=/opt/mmftp/venv/bin/python /opt/mmftp/app/manage.py send_queued_notifications
```

…with `mmftp-purge.service` (`… manage.py purge_expired_files`),
`mmftp-reminders.service` (`… manage.py send_expiry_reminders`), and
`mmftp-thumbnails.service` (`… manage.py generate_pending_thumbnails`) following the
same shape. Each has a matching `.timer`:
```ini
# /etc/systemd/system/mmftp-notifications.timer — every 2 minutes
[Unit]
Description=Drain the deferred-email queue every 2 minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=2min
Persistent=true
[Install]
WantedBy=timers.target

# /etc/systemd/system/mmftp-thumbnails.timer — every 2 min (same shape as notifications)
# /etc/systemd/system/mmftp-purge.timer — daily 03:00   -> OnCalendar=*-*-* 03:00:00
# /etc/systemd/system/mmftp-reminders.timer — daily 07:00 -> OnCalendar=*-*-* 07:00:00
```

> The `www-data` user must be able to read `/opt/mmftp`. After Step 7 run:
> `sudo chown -R www-data:www-data /opt/mmftp` (or use your own service user
> consistently in all units).

Start the web service and enable the **timers** (not the job services):
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mmftp-web \
     mmftp-notifications.timer mmftp-thumbnails.timer mmftp-purge.timer mmftp-reminders.timer
```

(The one-command installer `install_no_docker.sh` writes all of these for you.)

### Tuning concurrency — how many users at once

Two settings on the web service control how many requests are handled in
parallel. They are the **only** place concurrency is configured (not in the
code), so this is the knob to turn if the site ever feels slow:

```ini
# in mmftp-web.service, the ExecStart line:
--workers N        # N separate app processes — the main lever
--threads T        # threads per worker (helps when requests wait on DB/MinIO)
# total simultaneous requests ≈ N × T
```

Sizing rule of thumb (`workers = 2 × CPU cores + 1`):

| Server | Suggested ExecStart flags |
|---|---|
| 2 cores / 4 GB | `--workers 5 --timeout 600` |
| 4 cores / 8 GB | `--workers 9 --threads 2 --timeout 600` |
| 8 cores / 16 GB | `--workers 17 --threads 2 --timeout 600` |

Because file uploads/downloads stream straight to MinIO (they don't occupy a
web worker for the whole transfer), even a handful of workers serves a large
internal team comfortably. To change it:

```bash
sudo nano /etc/systemd/system/mmftp-web.service   # edit --workers / --threads
sudo systemctl daemon-reload
sudo systemctl restart mmftp-web
```

Background jobs no longer run on a worker: deferred email is drained by the
`mmftp-notifications.timer` (every 2 min) and cleanup/reminders by daily timers.
If the email queue ever backs up, shorten the timer interval
(`OnUnitActiveSec=`) or raise the per-run batch (`--limit`) on the drain command.
Thumbnails are generated inline on activation (no job).

---

## STEP 10 — Verify

```bash
# Web + infra running?
systemctl status postgresql nginx minio mmftp-web

# Background-job timers scheduled? (shows NEXT/LAST run per timer)
systemctl list-timers 'mmftp-*' --no-pager

# Health probe (DB + cache):
curl -s http://127.0.0.1:8000/healthz

# Logs if something is wrong:
journalctl -u mmftp-web -n 80 --no-pager
journalctl -u mmftp-notifications -n 80 --no-pager   # last drain run
```

Then open `http://<server-ip>/` in a browser, log in with the admin account from
Step 7, and do a **test upload and download** to confirm everything works.

**At this point the server can be disconnected from the internet — the app runs
fully offline.**

---

## Day-to-day operations

```bash
# Restart the web app after a code change (the job timers re-exec manage.py
# each run, so they pick up new code automatically — no restart needed):
sudo systemctl restart mmftp-web

# Run a background job by hand (e.g. to test it now instead of waiting for the timer):
sudo systemctl start mmftp-notifications.service   # or mmftp-thumbnails / mmftp-purge / mmftp-reminders

# Tail logs:
journalctl -u mmftp-web -f

# Backup the database:
sudo -u postgres pg_dump mmftp > mmftp-db-$(date +%F).sql

# Backup the files:
tar -czf mmftp-files-$(date +%F).tar.gz -C /opt/mmftp/minio-data .
```

**Applying a code update:**
```bash
cd /opt/mmftp/app
git pull                                   # or copy new code in
set -a; source /opt/mmftp/.env; set +a
/opt/mmftp/venv/bin/python manage.py migrate
/opt/mmftp/venv/bin/python manage.py collectstatic --noinput
sudo systemctl restart mmftp-web      # timers pick up new code on their next run
```

> `deploy/update_no_docker.sh` automates this and also retires the legacy Celery
> `mmftp-worker`/`mmftp-beat` units and installs the job timers if they aren't
> present yet.
>
> If the server is offline by update time, updating Python packages will need
> internet again (or a one-off `pip download` bundle). Code-only changes that
> don't add new packages update fine offline.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `DisallowedHost` error | Add the server IP/hostname to `DJANGO_ALLOWED_HOSTS`, restart web. |
| Login fails with CSRF error | `CSRF_TRUSTED_ORIGINS` must match scheme+host exactly (`http://` vs `https://`). |
| Upload OK but download 403 / `SignatureDoesNotMatch` | `MINIO_PUBLIC_ENDPOINT_URL` host ≠ the host the browser used. They must match exactly. |
| Download 404 at `/mmftp-files/...` | nginx `location /mmftp-files/` doesn't match `MINIO_BUCKET`. Align them, reload nginx. |
| DB auth failure | `.env` `POSTGRES_PASSWORD` must match what you set in Step 4. |
| Postgres "connection refused" on 5432, but `postgresql` shows active | The cluster is on a non-default port. Run `pg_lsclusters` — if it shows `5433`, another service held 5432 at install time. Fix: `sudo sed -i 's/^port = 5433/port = 5432/' /etc/postgresql/16/main/postgresql.conf && sudo systemctl restart postgresql@16-main`. (The umbrella `postgresql.service` reports "active" even when the cluster isn't serving — check `postgresql@16-main`.) |
| Any service won't start: "Address already in use" | Another program owns that port (6379/9000/5432/80). Find it: `sudo ss -ltnp \| grep :PORT`. Stop the conflicting service before starting ours. On a clean dedicated server this won't happen. |
| App can't reach MinIO | MinIO keys in `.env` must match the `minio.service` file. `systemctl status minio`. |
| Scheduled cleanup/expiry not running | `systemctl list-timers 'mmftp-*'` — timers must be active with a future NEXT. Inspect the last run: `journalctl -u mmftp-purge` (or `-reminders` / `-notifications`); failures are also tagged `mmftp-job` in the journal. |
| Queued emails not sending | `systemctl list-timers mmftp-notifications.timer`; run it now with `sudo systemctl start mmftp-notifications.service` and check `journalctl -u mmftp-notifications`. A misconfigured `.env`/venv shows up as an ExecStart error there. |

---

## Adding HTTPS later

1. Put the internal-CA cert/key on the server (e.g. `/etc/ssl/mmftp/`).
2. Add a `listen 443 ssl;` server block to the nginx config; redirect `:80 → :443`.
3. In `.env`: set `DJANGO_SECURE_SSL=True` and switch
   `MINIO_PUBLIC_ENDPOINT_URL` + `CSRF_TRUSTED_ORIGINS` to `https://...`.
4. `sudo nginx -t && sudo systemctl reload nginx` and
   `sudo systemctl restart mmftp-web`.
