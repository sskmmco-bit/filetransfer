"""Locust load test for MMFileTransfer.

Simulates a logged-in user browsing the app: dashboard, the three file lists,
the new debounced search (full page + AJAX partial), type filters, pagination,
and opening a file's detail page. Also exercises the public /healthz probe.

Auth: each simulated user logs in once in on_start() against the Django login
view, handling the CSRF token the same way a browser does (read the `csrftoken`
cookie / hidden field on the GET, echo it back as `csrfmiddlewaretoken` on the
POST). Successful logins do NOT count toward the login throttle, so reusing one
account across many users is fine — but see the throttle caveat in README.md.

Run (web stack already up on :8000):
    pip install -r requirements-dev.txt
    locust -f load_tests/locustfile.py --host http://localhost:8000
    # then open http://localhost:8089

Headless example:
    locust -f load_tests/locustfile.py --host http://localhost:8000 \
           --users 50 --spawn-rate 5 --run-time 5m --headless

Configurable via environment variables:
    LOCUST_USER       login identifier (default: admin)
    LOCUST_PASSWORD   password         (default: adminpass123)
    LOCUST_HOST       fallback host if --host is omitted
"""
from __future__ import annotations

import os
import re

from locust import HttpUser, between, task

LOGIN_PATH = "/accounts/login/"
USER = os.getenv("LOCUST_USER", "admin")
PASSWORD = os.getenv("LOCUST_PASSWORD", "adminpass123")

# Search terms the simulated users type into the debounced search box.
SEARCH_TERMS = ["report", "invoice", "photo", "draft", "2024", "notes", "final", "data"]
TYPE_FILTERS = ["doc", "img", "vid", "zip", "code", "other"]

# <a class="fname" href="/files/<uuid>/"> — used to pick a real file to open.
_DETAIL_HREF = re.compile(r'href="(/files/[0-9a-f-]{36}/)"')
_CSRF_INPUT = re.compile(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"')


class MMFTPUser(HttpUser):
    """A typical authenticated user clicking around the file lists."""

    # Realistic think-time between actions.
    wait_time = between(1, 5)
    # Works without --host; override on the CLI for other environments.
    host = os.getenv("LOCUST_HOST", "http://localhost:8000")

    def on_start(self):
        """Log in once per simulated user; stop the user if auth fails."""
        self.logged_in = False
        self._known_file_urls: list[str] = []
        self.login()

    # ----------------------------------------------------------------- auth
    def _csrf_token(self, html: str) -> str:
        """CSRF token to submit: prefer the cookie, fall back to the form field."""
        token = self.client.cookies.get("csrftoken")
        if token:
            return token
        m = _CSRF_INPUT.search(html)
        return m.group(1) if m else ""

    def login(self):
        # 1) GET the login page to seed the csrftoken cookie.
        with self.client.get(LOGIN_PATH, name="GET /accounts/login/", catch_response=True) as r:
            if r.status_code != 200:
                r.failure(f"login page returned {r.status_code}")
                self.environment.runner.quit()
                return
            token = self._csrf_token(r.text)

        # 2) POST credentials. Referer keeps Django happy on HTTPS deployments.
        with self.client.post(
            LOGIN_PATH,
            data={"username": USER, "password": PASSWORD, "csrfmiddlewaretoken": token},
            headers={"Referer": self.host + LOGIN_PATH},
            name="POST /accounts/login/",
            catch_response=True,
        ) as r:
            # A bounce back to the login form (still showing the password field)
            # means the credentials/CSRF were rejected.
            if r.status_code == 200 and 'name="password"' in r.text:
                r.failure("login rejected — check LOCUST_USER / LOCUST_PASSWORD or throttle")
                self.environment.runner.quit()
                return
            r.success()
            self.logged_in = True

    # -------------------------------------------------------------- browsing
    @task(5)
    def dashboard(self):
        self.client.get("/", name="GET / (dashboard)")

    @task(8)
    def my_uploads(self):
        r = self.client.get("/files/mine/", name="GET /files/mine/")
        # Remember a few real file URLs so view_file() can open one later.
        if r.status_code == 200:
            found = _DETAIL_HREF.findall(r.text)
            if found:
                self._known_file_urls = found[:10]

    @task(5)
    def assigned(self):
        self.client.get("/files/assigned/", name="GET /files/assigned/")

    @task(4)
    def starred(self):
        self.client.get("/files/starred/", name="GET /files/starred/")

    @task(10)
    def search_partial(self):
        """The hot path: debounced AJAX search hits the trigram-indexed query."""
        term = SEARCH_TERMS[self._rand_index(len(SEARCH_TERMS))]
        self.client.get(
            f"/files/mine/?q={term}&partial=1",
            headers={"X-Requested-With": "XMLHttpRequest"},
            name="GET /files/mine/?q=[term]&partial=1",
        )

    @task(3)
    def search_full_page(self):
        term = SEARCH_TERMS[self._rand_index(len(SEARCH_TERMS))]
        self.client.get(f"/files/mine/?q={term}", name="GET /files/mine/?q=[term]")

    @task(3)
    def type_filter(self):
        bucket = TYPE_FILTERS[self._rand_index(len(TYPE_FILTERS))]
        self.client.get(f"/files/mine/?type={bucket}", name="GET /files/mine/?type=[bucket]")

    @task(2)
    def paginate(self):
        self.client.get("/files/mine/?page=2", name="GET /files/mine/?page=N")

    @task(3)
    def view_file(self):
        """Open a real file detail page if we've seen one; else hit the list."""
        if self._known_file_urls:
            url = self._known_file_urls[self._rand_index(len(self._known_file_urls))]
            self.client.get(url, name="GET /files/[uuid]/")
        else:
            self.client.get("/files/mine/", name="GET /files/mine/")

    @task(1)
    def healthz(self):
        self.client.get("/healthz", name="GET /healthz")

    # ------------------------------------------------------------- utilities
    def _rand_index(self, n: int) -> int:
        """Cheap index varied per simulated user without importing random
        (keeps runs reproducible-ish and avoids global RNG contention)."""
        self._tick = getattr(self, "_tick", id(self) & 0xFFFF) + 1
        return self._tick % n
