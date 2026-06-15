# MMFileTransfer — Air-Gapped Deployment Guide

This document is for the infra team. The dev team will hand you two files:

* `mmftp-images.tar` — all Docker images (app + postgres + redis + minio + mc + nginx)
* `mmftp-deploy.tar.gz` — compose file, nginx configs, env template, this README, install script

You will run the stack on an Ubuntu 22.04 / 24.04 LTS server with **no internet**.

> One application image (`mmftp_app`) runs three roles — **web** (Gunicorn),
> **worker** (Celery), and **beat** (Celery scheduler). Plus PostgreSQL, Redis,
> MinIO (object storage), and nginx. All traffic enters through a single host
> nginx on port 80 (TLS-ready); nothing else is published to the network.

---

## 1. Server Specs

| Resource | Minimum            | Recommended         |
| -------- | ------------------ | ------------------- |
| CPU      | 2 vCPU             | 4 vCPU              |
| RAM      | 4 GB               | 8 GB                |
| Disk     | 40 GB SSD          | 200 GB SSD          |
| OS       | Ubuntu 22.04 LTS   | Ubuntu 24.04 LTS    |
| Network  | Host nginx on :80  | + :443 with TLS later |

Disk breakdown:

* Docker images: ~1.5 GB
* PostgreSQL data: small (metadata only) — starts ~1 GB
* MinIO data (the actual transferred files): grows fastest — plan headroom

Prerequisites on the server (must already be installed — it has no internet):

* Docker Engine 24+
* Docker Compose plugin v2+
* nginx (host-level reverse proxy)
* `tar`, `bash` (usually present)

Verify:

```bash
docker --version
docker compose version
nginx -v
```

---

## 2. Build the Package (dev side, on a machine WITH internet)

From the repo root:

```powershell
pwsh -File .\scripts\build-deploy-package.ps1
```

This builds `mmftp_app`, pulls the third-party images, and writes to `.\dist`:

* `mmftp-images.tar`
* `mmftp-deploy.tar.gz`

For a **code-only update** later (postgres/redis/minio/nginx already on the
server), use `-AppOnly` to ship just the app image:

```powershell
pwsh -File .\scripts\build-deploy-package.ps1 -AppOnly
```

---

## 3. Prepare the Install Directory (server side)

```bash
sudo mkdir -p /opt/mmftp
sudo chown -R $USER:$USER /opt/mmftp
```

---

## 4. Copy the Two Files Across

Transfer (USB / scp from a jump host / your air-gap workflow):

```text
mmftp-images.tar       -> /opt/mmftp/
mmftp-deploy.tar.gz    -> /opt/mmftp/
```

---

## 5. Extract the Deploy Bundle

```bash
cd /opt/mmftp
tar -xzf mmftp-deploy.tar.gz
```

After extraction:

```text
/opt/mmftp/
├── mmftp-images.tar
├── docker-compose.airgap.yml
├── README_DEPLOY.md
├── .env.production.template
└── deploy/
    ├── nginx.compose.conf
    ├── nginx.host.conf
    └── install.sh
```

---

## 6. Option A — Automated Install (recommended)

```bash
sudo bash /opt/mmftp/deploy/install.sh
```

First run will:

1. Load Docker images from `mmftp-images.tar`
2. Copy `.env.production.template` → `.env.production` and stop, asking you to edit it
3. (After you edit and re-run) Start the containers
4. Install + reload the host nginx config

Edit the env file when the script tells you to:

```bash
sudo nano /opt/mmftp/.env.production
```

Replace every `CHANGE_ME_*` value (see Section 8). Then re-run the script.

The app's web container automatically applies migrations, collects static, and
creates the superuser from `DJANGO_SUPERUSER_*`; a one-shot job creates the
MinIO bucket. No manual seed/migrate step is needed.

---

## 7. Option B — Manual Install

```bash
cd /opt/mmftp
docker load -i mmftp-images.tar
docker images        # expect mmftp_app, postgres, redis, minio/minio, minio/mc, nginx

cp .env.production.template .env.production
chmod 600 .env.production
nano .env.production       # replace every CHANGE_ME_* (Section 8)

docker compose --env-file .env.production -f docker-compose.airgap.yml up -d

sudo cp deploy/nginx.host.conf /etc/nginx/sites-available/mmftp
sudo ln -sf /etc/nginx/sites-available/mmftp /etc/nginx/sites-enabled/mmftp
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

---

## 8. Environment Variables — What to Change

Open `/opt/mmftp/.env.production`. Every `CHANGE_ME_*` must be replaced:

| Variable                    | What to set                                                            |
| --------------------------- | --------------------------------------------------------------------- |
| `DJANGO_SECRET_KEY`         | Long random string: `python3 -c "import secrets;print(secrets.token_urlsafe(64))"` |
| `DJANGO_ALLOWED_HOSTS`      | Replace `CHANGE_ME_SERVER_IP` with the server's IP (keep the rest)    |
| `CSRF_TRUSTED_ORIGINS`      | `http://<server-ip>` (switch to `https://` when TLS is on)            |
| `SECRETS_ENCRYPTION_KEY`    | Fernet key: `openssl rand -base64 32 \| tr '+/' '-_'`                 |
| `DJANGO_SUPERUSER_PASSWORD` | Strong admin password (used for the first login)                     |
| `POSTGRES_PASSWORD`         | Strong DB password                                                    |
| `MINIO_ACCESS_KEY`          | MinIO username (any value)                                            |
| `MINIO_SECRET_KEY`          | MinIO password, min 8 chars                                           |
| `MINIO_PUBLIC_ENDPOINT_URL` | `http://<server-ip>` — the URL the browser uses to download files    |

> **Important — `MINIO_PUBLIC_ENDPOINT_URL`.** The app hands the browser
> *presigned* download links. The signature is bound to this URL's host, so it
> **must** be the exact address users hit (the server IP, or the domain/https
> once TLS is on). Files are proxied through nginx at `/<bucket>/`, so this is
> just the base URL — no separate MinIO port is exposed.

If you change `MINIO_BUCKET` away from `mmftp-files`, also change the
`location /mmftp-files/` line in `deploy/nginx.compose.conf` to match.

---

## 9. Verify

```bash
cd /opt/mmftp
docker compose --env-file .env.production -f docker-compose.airgap.yml ps
```

`web`, `worker`, `beat`, `postgres`, `redis`, `minio`, `nginx` should be `Up`
(`createbuckets` exits 0 after making the bucket — that's expected). Then:

```bash
docker compose --env-file .env.production -f docker-compose.airgap.yml logs --tail 80 web
```

Look for `applying migrations`, `collecting static`, `ensuring superuser`, and
the Gunicorn `Listening at` line. Check the health probe:

```bash
curl -s http://127.0.0.1:8080/healthz        # DB + Redis probe, should be OK
```

Then open `http://<server-ip>/` in a browser and log in with
`DJANGO_SUPERUSER_USERNAME` / `DJANGO_SUPERUSER_PASSWORD`. Do a test upload and
download to confirm MinIO presigned URLs resolve.

---

## 10. Operational Commands

All commands run from `/opt/mmftp`. Define a shortcut:

```bash
alias dc='docker compose --env-file /opt/mmftp/.env.production -f /opt/mmftp/docker-compose.airgap.yml'
```

```bash
dc ps                       # status
dc logs -f web worker beat  # tail app logs
dc restart web              # restart one service
dc down                     # stop the stack (data preserved)
dc down -v                  # stop + WIPE ALL DATA (destructive)
```

### Backup PostgreSQL
```bash
docker exec mmftp-postgres-1 pg_dump -U mmftp mmftp > mmftp-db-$(date +%F).sql
```

### Backup MinIO objects (the transferred files)
```bash
docker run --rm -v mmftp_minio_data:/data -v $PWD:/backup alpine \
  tar -czf /backup/mmftp-minio-$(date +%F).tar.gz -C /data .
```

> Container/volume names are prefixed with the compose project name (the install
> dir, `mmftp`). Confirm exact names with `docker ps` / `docker volume ls`.

### Apply a code-only update
```bash
docker load -i mmftp-app-image.tar
dc up -d           # recreates web/worker/beat from the new image; migrations auto-apply
```

---

## 11. Networking — How Traffic Flows

```
Browser
   │  http://<server-ip>/   (and https:// later)
   ▼
Host nginx (:80 / :443, TLS)          /etc/nginx/sites-enabled/mmftp
   │  proxy_pass 127.0.0.1:8080
   ▼
nginx container (127.0.0.1:8080)      deploy/nginx.compose.conf
   ├─ /static/         → static volume
   ├─ /mmftp-files/    → minio:9000     (presigned download/preview URLs)
   └─ /                → web:8000 (Gunicorn)
                            │
   web / worker / beat ─────┼─► postgres:5432   (mmftp_net)
                            ├─► redis:6379      (mmftp_net)
                            └─► minio:9000      (mmftp_net)
```

postgres, redis, minio, and the app are **not** published to the host. Only the
host nginx (:80) and the container nginx loopback (127.0.0.1:8080) are bound.

---

## 12. Adding HTTPS Later

Internal CA cert for the server is recommended. Once you have the cert + key:

1. Place them at `/etc/ssl/mmftp/server.crt` and `/etc/ssl/mmftp/server.key`.
2. In `/etc/nginx/sites-available/mmftp` uncomment the `:443` block and change
   the `:80` block's `location /` body to `return 301 https://$host$request_uri;`.
3. `sudo nginx -t && sudo systemctl reload nginx`.
4. In `.env.production` set:
   * `DJANGO_SECURE_SSL=True`
   * `MINIO_PUBLIC_ENDPOINT_URL=https://<server-ip-or-domain>`
   * `CSRF_TRUSTED_ORIGINS=https://<server-ip-or-domain>`
5. `dc up -d web worker beat`

---

## 13. Troubleshooting

| Symptom                                   | Check                                                                                          |
| ----------------------------------------- | --------------------------------------------------------------------------------------------- |
| `install.sh` aborts: "CHANGE_ME_ placeholders" | You haven't filled in `.env.production`. Edit it, re-run.                                  |
| `web` restarts, logs show DB auth failure | `POSTGRES_PASSWORD` only takes effect on **first** init of an empty `pg_data` volume. Either keep the original password, or `dc down -v` (DESTROYS DATA), or `ALTER USER` inside psql then update the env. |
| Login page loads but pages 400 / DisallowedHost | `DJANGO_ALLOWED_HOSTS` missing the server IP/domain.                                     |
| Login POST fails with CSRF error          | `CSRF_TRUSTED_ORIGINS` doesn't match the scheme+host (`http://` vs `https://`).                |
| Upload works but download 403 / SignatureDoesNotMatch | `MINIO_PUBLIC_ENDPOINT_URL` host ≠ the host the browser used. They must match exactly. |
| Download 404 at `/mmftp-files/...`        | `location` in `nginx.compose.conf` doesn't match `MINIO_BUCKET`. Align them, `dc restart nginx`. |
| Audit log shows the proxy IP, not the client | Check `TRUSTED_PROXY_IPS` — it must list the nginx container IP (172.28.0.10) and gateway (172.28.0.1). |
| Scheduled cleanup/expiry not running      | `dc logs beat` and `dc logs worker`; verify both are `Up`.                                     |
| `host nginx: address already in use`      | Another service holds :80 — `sudo ss -ltnp \| grep :80`.                                       |
| Need the MinIO web console for debugging  | Uncomment the `ports:` under `minio:` in the compose, `dc up -d minio`, browse 127.0.0.1:9001. |
