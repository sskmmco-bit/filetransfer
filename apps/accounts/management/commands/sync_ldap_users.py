"""Inspect the LDAP directory and optionally add specific users (§5.7.3).

The primary way a directory user becomes a local account is just-in-time on
their first login: `MultiIdentifierBackend` checks the local users first, then
binds against LDAP, and only then provisions that one user. This command does
NOT bulk-import everyone — it is a convenience for admins:

  * with no arguments it only LISTS the directory users (read-only, writes
    nothing) so you can see who is there;
  * with --add USERNAME [...] it provisions just those named users ahead of
    their first login, using the exact same path as JIT.

Provisioned accounts are active, get the default LDAP role, an unusable local
password (auth_source=LDAP), and no explicit quota — so they inherit the site
default quota (§quota). A name that collides with a local (non-LDAP) account is
skipped, because local accounts always win (§5.7.1a).

Usage:
    python manage.py sync_ldap_users                      # list directory users
    python manage.py sync_ldap_users --add jdoe           # add one user
    python manage.py sync_ldap_users --add jdoe asmith    # add several
    python manage.py sync_ldap_users --username-attr sAMAccountName   # Active Directory
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.ldap_provision import provision_or_sync_ldap_user
from apps.config.models import SiteSettings


class Command(BaseCommand):
    help = "List LDAP directory users, or add specific ones with --add. Never bulk-imports."

    def add_arguments(self, parser):
        parser.add_argument(
            "--add", nargs="+", metavar="USERNAME",
            help="Provision these specific directory users (by login username). "
                 "Without --add, the command only lists users and writes nothing.",
        )
        parser.add_argument(
            "--filter", dest="search_filter",
            help="LDAP search filter used when LISTING. Defaults to the site "
                 "user-search filter with '*' in place of the username "
                 "(e.g. '(uid=%%(user)s)' => '(uid=*)').",
        )
        parser.add_argument(
            "--username-attr", dest="username_attr", default="uid",
            help="Directory attribute used as the login username (default: uid; "
                 "use sAMAccountName for Active Directory).",
        )

    def handle(self, *args, **opts):
        s = SiteSettings.get()
        if not s.ldap_enabled or not s.ldap_server_uri:
            raise CommandError("LDAP is not enabled / configured in Site Settings.")
        if not s.ldap_bind_dn:
            raise CommandError(
                "A service bind DN + password is required to query the directory "
                "(set ldap_bind_dn / bind password in Site Settings)."
            )

        try:
            import ldap3  # local import: optional dependency
        except ImportError as exc:
            raise CommandError(f"ldap3 is not installed: {exc}")

        username_attr = opts["username_attr"]
        to_add = opts.get("add")
        if to_add:
            self._add_users(s, ldap3, to_add, username_attr)
        else:
            search_filter = opts.get("search_filter") or self._default_filter(s)
            self._list_users(s, ldap3, search_filter, username_attr)

    # ------------------------------------------------------------------
    def _list_users(self, s, ldap3, search_filter, username_attr):
        entries = self._search(s, ldap3, search_filter, username_attr)
        for username, attrs in entries:
            self.stdout.write(f"  {username} <{attrs.get('email') or '?'}>")
        self.stdout.write(
            f"LDAP returned {len(entries)} user(s) for {search_filter!r}. "
            "Listing only — run with --add USERNAME to provision a user."
        )

    def _add_users(self, s, ldap3, usernames, username_attr):
        # Per-user lookup with the configured login filter — the same query JIT
        # uses — so adding ahead of time matches first-login behaviour exactly.
        login_filter = s.ldap_user_search_filter or "(uid=%(user)s)"
        created = synced = skipped = missing = 0
        for name in usernames:
            try:
                per_user_filter = login_filter % {"user": name}
            except (KeyError, ValueError):
                per_user_filter = f"({username_attr}={name})"
            matches = self._search(s, ldap3, per_user_filter, username_attr)
            if not matches:
                missing += 1
                self.stderr.write(f"  not found in directory: {name}")
                continue
            for found_name, attrs in matches:
                user, was_created = provision_or_sync_ldap_user(s, found_name, attrs)
                if user is None:
                    skipped += 1
                    self.stderr.write(f"  skipped (local collision): {found_name}")
                elif was_created:
                    created += 1
                    self.stdout.write(f"  added: {found_name} <{attrs.get('email')}>")
                else:
                    synced += 1
                    self.stdout.write(f"  refreshed: {found_name} <{attrs.get('email')}>")
        self.stdout.write(self.style.SUCCESS(
            f"Done. added={created} refreshed={synced} skipped={skipped} not_found={missing}"
        ))

    # ------------------------------------------------------------------
    def _default_filter(self, s) -> str:
        """Turn the per-user login filter into an all-users filter for listing.

        '(uid=%(user)s)' => '(uid=*)'. Falls back to '(objectClass=person)' when
        no usable filter is configured.
        """
        base = s.ldap_user_search_filter or ""
        if "%(user)s" in base:
            return base.replace("%(user)s", "*")
        return base or "(objectClass=person)"

    def _search(self, s, ldap3, search_filter: str, username_attr: str):
        """Service-bind, page through the directory, and return [(username, attrs)]."""
        server = ldap3.Server(
            s.ldap_server_uri,
            use_ssl=s.ldap_use_ssl,
            connect_timeout=s.ldap_connect_timeout or 5,
            get_info=ldap3.NONE,
        )
        wanted = [username_attr] + [
            a for a in (
                s.ldap_attr_email,
                s.ldap_attr_employee_id,
                s.ldap_attr_first_name,
                s.ldap_attr_last_name,
            ) if a
        ]
        try:
            conn = ldap3.Connection(
                server,
                user=s.ldap_bind_dn,
                password=s.get_ldap_bind_password(),
                auto_bind=True,
                receive_timeout=s.ldap_connect_timeout or 5,
            )
        except Exception as exc:  # noqa: BLE001
            raise CommandError(f"LDAP service bind failed: {exc}")

        results = []
        try:
            entry_gen = conn.extend.standard.paged_search(
                search_base=s.ldap_base_dn,
                search_filter=search_filter,
                attributes=wanted,
                paged_size=500,
                generator=True,
            )
            for entry in entry_gen:
                if entry.get("type") != "searchResEntry":
                    continue
                a = entry.get("attributes", {})
                username = _first(a.get(username_attr))
                if not username:
                    continue
                email = _first(a.get(s.ldap_attr_email))
                if not email:
                    # email is the minimum required attribute (§5.7.3).
                    self.stderr.write(f"  skipped (no email): {username}")
                    continue
                results.append((username, {
                    "email": email,
                    "employee_id": _first(a.get(s.ldap_attr_employee_id)),
                    "first_name": _first(a.get(s.ldap_attr_first_name)),
                    "last_name": _first(a.get(s.ldap_attr_last_name)),
                }))
        finally:
            conn.unbind()
        return results


def _first(val) -> str:
    """LDAP attributes come back as lists; take the first scalar as a string."""
    if isinstance(val, (list, tuple)):
        val = val[0] if val else ""
    return str(val or "").strip()
