"""Authentication backends (§5.7.1, §5.7.3).

MultiIdentifierBackend resolves the typed identifier against username, email,
or employee_id. Resolution rules (§5.7.1a):

  * A local account always wins. If a local user resolves, ONLY the local
    password is tried — there is no LDAP fallback for that account.
  * Otherwise, if LDAP is enabled, the directory is consulted. On a genuine
    first login (bind OK, no colliding local row) a User is provisioned
    just-in-time with the Uploader role. An existing LDAP user has their
    profile attributes synced. Attributes that collide with a local row mean
    NO JIT account is created and login fails generically (local wins).
"""
from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q

logger = logging.getLogger(__name__)
UserModel = get_user_model()


class MultiIdentifierBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        identifier = (username or kwargs.get("identifier") or "").strip()
        if not identifier or password is None:
            return None

        user = self._resolve_local(identifier)

        # Local account: local password only, no LDAP fallback (§5.7.1a).
        if user is not None:
            if user.check_password(password) and self.user_can_authenticate(user):
                return user
            return None

        # No local match — try LDAP if configured.
        return self._authenticate_ldap(identifier, password)

    # ------------------------------------------------------------------
    def _resolve_local(self, identifier: str):
        try:
            return UserModel.objects.get(
                Q(username__iexact=identifier)
                | Q(email__iexact=identifier)
                | Q(employee_id__iexact=identifier)
            )
        except UserModel.DoesNotExist:
            # Hash once to blunt timing-based user enumeration.
            UserModel().set_password(identifier)
            return None
        except UserModel.MultipleObjectsReturned:
            # Ambiguous identifier — prefer an exact username match.
            return UserModel.objects.filter(username__iexact=identifier).first()

    # ------------------------------------------------------------------
    def _authenticate_ldap(self, identifier: str, password: str):
        from apps.config.models import SiteSettings

        settings_obj = SiteSettings.get()
        if not settings_obj.ldap_enabled or not settings_obj.ldap_server_uri:
            return None

        try:
            attrs = self._ldap_bind_and_fetch(settings_obj, identifier, password)
        except Exception as exc:  # noqa: BLE001 — never leak directory errors to the user
            logger.warning("LDAP authentication error for %r: %s", identifier, exc)
            return None

        if attrs is None:
            return None  # bind failed / required attrs missing

        return self._provision_or_sync(settings_obj, identifier, attrs)

    def _ldap_bind_and_fetch(self, s, identifier: str, password: str):
        """Bind as the user and return mapped attributes, or None on failure."""
        import ldap3  # local import: optional dependency

        server = ldap3.Server(
            s.ldap_server_uri,
            use_ssl=s.ldap_use_ssl,
            connect_timeout=s.ldap_connect_timeout or 5,
            get_info=ldap3.NONE,
        )

        # Step 1: locate the user's DN using a service bind (if configured) or anonymously.
        search_filter = (s.ldap_user_search_filter or "(uid=%(user)s)") % {"user": identifier}
        bind_dn = s.ldap_bind_dn or None
        bind_pw = s.get_ldap_bind_password() if bind_dn else None

        wanted = [
            a for a in (
                s.ldap_attr_email,
                s.ldap_attr_employee_id,
                s.ldap_attr_first_name,
                s.ldap_attr_last_name,
            ) if a
        ]

        conn = ldap3.Connection(
            server, user=bind_dn, password=bind_pw, auto_bind=True,
            receive_timeout=s.ldap_connect_timeout or 5,
        )
        try:
            if not conn.search(s.ldap_base_dn, search_filter, attributes=wanted):
                return None
            if not conn.entries:
                return None
            entry = conn.entries[0]
            user_dn = entry.entry_dn
        finally:
            conn.unbind()

        # Step 2: bind as the user with their password to verify credentials.
        user_conn = ldap3.Connection(server, user=user_dn, password=password)
        if not user_conn.bind():
            return None
        user_conn.unbind()

        def _attr(name):
            if not name or not hasattr(entry, name):
                return ""
            val = getattr(entry, name).value
            if isinstance(val, (list, tuple)):
                val = val[0] if val else ""
            return str(val or "")

        mapped = {
            "email": _attr(s.ldap_attr_email),
            "employee_id": _attr(s.ldap_attr_employee_id),
            "first_name": _attr(s.ldap_attr_first_name),
            "last_name": _attr(s.ldap_attr_last_name),
        }
        # Required attributes present? (§5.7.3 — email is the minimum.)
        if not mapped["email"]:
            logger.info("LDAP login for %r rejected: required attributes missing", identifier)
            return None
        return mapped

    def _provision_or_sync(self, s, identifier: str, attrs: dict):
        from apps.accounts.models import AuthSource, Role, RoleSlug
        from apps.audit.models import ActivityAction, ActivityLog

        email = attrs["email"]
        employee_id = attrs["employee_id"] or None

        # Already provisioned from LDAP before? Sync and return.
        existing_ldap = UserModel.objects.filter(
            username__iexact=identifier, auth_source=AuthSource.LDAP
        ).first()
        if existing_ldap:
            existing_ldap.email = email
            if employee_id:
                existing_ldap.employee_id = employee_id
            existing_ldap.first_name = attrs["first_name"]
            existing_ldap.last_name = attrs["last_name"]
            existing_ldap.save(update_fields=["email", "employee_id", "first_name", "last_name"])
            return existing_ldap

        # Collision with a local row by email or employee_id => NO JIT (local wins).
        collision = Q(email__iexact=email)
        if employee_id:
            collision |= Q(employee_id__iexact=employee_id)
        collision |= Q(username__iexact=identifier)
        if UserModel.objects.filter(collision).exists():
            logger.info("LDAP JIT for %r blocked: attributes collide with a local row", identifier)
            return None

        # Genuine first login — provision with the Uploader role.
        role = Role.objects.filter(slug=RoleSlug.UPLOADER).first()
        user = UserModel(
            username=identifier,
            email=email,
            employee_id=employee_id,
            first_name=attrs["first_name"],
            last_name=attrs["last_name"],
            auth_source=AuthSource.LDAP,
            role=role,
            is_active=True,
        )
        user.set_unusable_password()  # LDAP users never have a local password
        user.save()
        ActivityLog.objects.create(
            actor=user,
            action=ActivityAction.USER_CREATED,
            message=f"JIT-provisioned from LDAP as Uploader ({email})",
        )
        try:
            from apps.notifications.tasks import enqueue_welcome_email

            enqueue_welcome_email(user)
        except Exception:  # noqa: BLE001 — welcome mail is best-effort
            pass
        return user
