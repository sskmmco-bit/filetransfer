"""Bulk-import / sync users from the configured LDAP directory (§5.7.3).

Where the login backend provisions a directory user just-in-time on their first
login, this command pulls them in ahead of time: it binds with the configured
service account, searches the directory, and provisions every matching entry as
a local account so they appear in the user list and can log in immediately.

Each new account is created active, with the Uploader role, an unusable local
password (auth_source=LDAP), and no explicit quota — so it inherits the site
default quota (§quota). Accounts already provisioned from LDAP have their
attributes refreshed; entries that collide with a local (non-LDAP) account are
skipped, because local accounts always win (§5.7.1a).

Usage:
    python manage.py sync_ldap_users --dry-run
    python manage.py sync_ldap_users
    python manage.py sync_ldap_users --filter "(objectClass=person)" --username-attr sAMAccountName
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.ldap_provision import provision_or_sync_ldap_user
from apps.config.models import SiteSettings


class Command(BaseCommand):
    help = "Import / sync users from the configured LDAP directory as Uploaders."

    def add_arguments(self, parser):
        parser.add_argument(
            "--filter", dest="search_filter",
            help="LDAP search filter for the users to import. Defaults to the "
                 "site user-search filter with '*' in place of the username "
                 "(e.g. '(uid=%%(user)s)' => '(uid=*)').",
        )
        parser.add_argument(
            "--username-attr", dest="username_attr", default="uid",
            help="Directory attribute used as the login username (default: uid; "
                 "use sAMAccountName for Active Directory).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would change without writing anything.",
        )

    def handle(self, *args, **opts):
        s = SiteSettings.get()
        if not s.ldap_enabled or not s.ldap_server_uri:
            raise CommandError("LDAP is not enabled / configured in Site Settings.")
        if not s.ldap_bind_dn:
            raise CommandError(
                "A service bind DN + password is required for bulk import "
                "(set ldap_bind_dn / bind password in Site Settings)."
            )

        try:
            import ldap3  # local import: optional dependency
        except ImportError as exc:
            raise CommandError(f"ldap3 is not installed: {exc}")

        username_attr = opts["username_attr"]
        search_filter = opts.get("search_filter") or self._default_filter(s)
        dry_run = opts["dry_run"]

        entries = self._search(s, ldap3, search_filter, username_attr)
        self.stdout.write(
            f"LDAP returned {len(entries)} matching entr"
            f"{'y' if len(entries) == 1 else 'ies'} for {search_filter!r}."
        )

        created = synced = skipped = 0
        for username, attrs in entries:
            if dry_run:
                self.stdout.write(f"  would sync: {username} <{attrs.get('email') or '?'}>")
                continue
            user, was_created = provision_or_sync_ldap_user(s, username, attrs)
            if user is None:
                skipped += 1
                self.stderr.write(f"  skipped (local collision): {username}")
            elif was_created:
                created += 1
            else:
                synced += 1

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — no changes written."))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Done. created={created} synced={synced} skipped={skipped}"
            ))

    # ------------------------------------------------------------------
    def _default_filter(self, s) -> str:
        """Turn the per-user login filter into an all-users filter.

        '(uid=%(user)s)' => '(uid=*)'. Falls back to '(objectClass=person)' when
        no usable filter is configured.
        """
        base = s.ldap_user_search_filter or ""
        if "%(user)s" in base:
            return base.replace("%(user)s", "*")
        return base or "(objectClass=person)"

    def _search(self, s, ldap3, search_filter: str, username_attr: str):
        """Service-bind, page through the directory, and return (username, attrs)."""
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
