"""Shared LDAP provisioning (§5.7.3).

One source of truth for turning a directory entry into a local account, used by
both the just-in-time login path (`MultiIdentifierBackend`) and the bulk
`sync_ldap_users` management command. No `ldap3` import lives here — callers do
the binding/search and hand us already-mapped attributes, so this module is a
pure DB operation and is safe to import everywhere.

Resolution rules mirror §5.7.1a: an LDAP entry that collides with a local
(non-LDAP) row is skipped — local accounts always win. New accounts are created
active, with the Uploader role, an unusable local password, and `quota_bytes`
left NULL so they inherit the site default quota (§quota).
"""
from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from django.db.models import Q

logger = logging.getLogger(__name__)
UserModel = get_user_model()


def provision_or_sync_ldap_user(settings_obj, identifier: str, attrs: dict):
    """Create or refresh the local account for one LDAP entry.

    `identifier` is the login username (the directory's username attribute).
    `attrs` must carry email/employee_id/first_name/last_name.

    Returns ``(user, created)`` — `created` is True only when a brand-new row was
    provisioned. Returns ``(None, False)`` when a local-row collision blocks
    provisioning.
    """
    from apps.accounts.models import AuthSource, Role, RoleSlug
    from apps.audit.models import ActivityAction, ActivityLog

    email = attrs.get("email") or ""
    employee_id = attrs.get("employee_id") or None

    # Already provisioned from LDAP before? Sync attributes and return.
    existing_ldap = UserModel.objects.filter(
        username__iexact=identifier, auth_source=AuthSource.LDAP
    ).first()
    if existing_ldap:
        existing_ldap.email = email
        if employee_id:
            existing_ldap.employee_id = employee_id
        existing_ldap.first_name = attrs.get("first_name", "")
        existing_ldap.last_name = attrs.get("last_name", "")
        existing_ldap.save(
            update_fields=["email", "employee_id", "first_name", "last_name"]
        )
        return existing_ldap, False

    # Collision with a local row by email / employee_id / username => NO JIT
    # (local wins, §5.7.1a).
    collision = Q(username__iexact=identifier)
    if email:
        collision |= Q(email__iexact=email)
    if employee_id:
        collision |= Q(employee_id__iexact=employee_id)
    if UserModel.objects.filter(collision).exists():
        logger.info(
            "LDAP provisioning for %r blocked: attributes collide with a local row",
            identifier,
        )
        return None, False

    # Genuine first sight of this user — provision with the Uploader role.
    role = Role.objects.filter(slug=RoleSlug.UPLOADER).first()
    user = UserModel(
        username=identifier,
        email=email,
        employee_id=employee_id,
        first_name=attrs.get("first_name", ""),
        last_name=attrs.get("last_name", ""),
        auth_source=AuthSource.LDAP,
        role=role,
        is_active=True,
    )
    user.set_unusable_password()  # LDAP users never have a local password
    user.save()
    ActivityLog.objects.create(
        actor=user,
        action=ActivityAction.USER_CREATED,
        message=f"Provisioned from LDAP as Uploader ({email})",
    )
    try:
        from apps.notifications.tasks import enqueue_welcome_email

        enqueue_welcome_email(user)
    except Exception:  # noqa: BLE001 — welcome mail is best-effort
        pass
    return user, True
