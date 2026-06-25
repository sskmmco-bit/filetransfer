# Load testing with Locust

[`locustfile.py`](locustfile.py) drives a realistic authenticated session:
dashboard → file lists (My Files / Shared with Me / Starred) → the debounced
search (both the full page and the AJAX `?partial=1` path that hits the trigram
index) → type filters → pagination → opening a file's detail page, plus the
public `/healthz` probe. Task weights approximate read-heavy real usage, with
search weighted highest since it's the newest hot path.

Each simulated user logs in once against `/accounts/login/`, handling the CSRF
token exactly like a browser.

## Running Locust

Point it at a running instance of the app (`runserver` for dev, or Gunicorn
behind nginx for realistic numbers).

```bash
pip install -r requirements-dev.txt        # installs locust (+ app deps)
locust -f load_tests/locustfile.py --host http://localhost:8000
```

Open <http://localhost:8089>, set the number of users + spawn rate, and start.

Headless (CI-friendly — runs 5 min, writes a CSV report, exits non-zero if any
request fails):

```bash
locust -f load_tests/locustfile.py --host http://localhost:8000 \
  --users 50 --spawn-rate 5 --run-time 5m --headless \
  --csv load_tests/report --exit-code-on-error 1
```

## Testing against the production-like stack (Gunicorn + nginx)

The dev `runserver` (single-threaded, `DEBUG=True`) is **not** representative.
For real throughput numbers, load-test against the production-like stack —
Gunicorn (multiple workers, `DEBUG=False`) behind nginx, as set up by
`deploy/INSTALL_NO_DOCKER.md`.

Point `--host` at the server: `http://<server>/` exercises the **full edge**
through nginx, while `http://<server>:8000` (if Gunicorn's port is reachable)
hits the app tier directly.

> Hitting Gunicorn directly isolates app capacity (DB, Django, worker
> count) — usually what you want to measure. nginx adds
> proxying/buffering/keepalive but is rarely the bottleneck for dynamic
> requests. Test through nginx when you specifically want to validate the
> whole edge (e.g. timeouts, `client_max_body_size`, connection limits).

## Credentials

Defaults to the bootstrap superuser `admin` / `adminpass123`. Override with env
vars:

```bash
LOCUST_USER=someuser LOCUST_PASSWORD=secret \
  locust -f load_tests/locustfile.py --host http://localhost:8000
```

## ⚠️ Login throttle — read before a big run

`apps/accounts/security.py` hard-blocks auth attempts (15-minute window):

- **5** failed attempts per identifier → that identifier is blocked
- **20** failed attempts per IP → the whole IP is blocked

**Successful** logins don't count, so reusing one account across many users is
fine. But all Locust traffic comes from a single source IP, so if logins start
**failing** (wrong password, app returning 500s under load, CSRF issues), you
can trip the 20-per-IP block and every subsequent login fails — which looks like
an app failure but isn't. The locustfile calls `runner.quit()` on a rejected
login to surface this fast rather than hammering the throttle.

If you need that many *distinct* accounts, pre-create test users (e.g. a
management command or the admin console) and extend the locustfile to pick from
a pool instead of the single `LOCUST_USER`.

## Notes & caveats

- **Read-only by design.** The script does not upload, share, or delete — it
  won't mutate data or write blobs to MinIO. Add `@task`s with proper CSRF
  headers if you want to load-test writes.
- **Downloads** aren't exercised: `/files/<uuid>/download/` mints a presigned
  URL and redirects to MinIO, so it would load-test MinIO, not Django. Add it
  deliberately if that's your goal.
- **`per_page` cookie / adaptive page size**: the first list view a browser
  loads may redirect to set `per_page`; Locust ignores that and uses the default
  page size, which is fine for steady-state load.
- Point `--host` at a staging/perf environment for meaningful numbers — the
  dev `runserver` (single-threaded, `DEBUG=True`) is **not** representative.
  Use the prod-like stack (Gunicorn + nginx, per `deploy/INSTALL_NO_DOCKER.md`)
  to measure real throughput.
```
