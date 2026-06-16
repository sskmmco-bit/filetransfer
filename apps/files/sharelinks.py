"""Share-link lifecycle (§5.4).

A `ShareLink` is a permission wrapper around one or more files: it owns the
public controls (password, email-verify, preview-only, expiry, download limit)
and the running view/download counters. The same file can belong to several
links. `apps/public` resolves a link by its token; this module is the only
place that mints/mutates links and keeps the denormalised `StoredFile.is_public`
/ `public_token` hints (used by the file-list "Public" badge) in sync.
"""
from __future__ import annotations

import secrets

from django.contrib.auth.hashers import make_password
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import ShareLink, ShareLinkFile, StoredFile

# Sentinel: distinguishes "leave the password unchanged" (KEEP) from "" (clear).
KEEP = object()


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def normalize_emails(value) -> list:
    """Accept a list or a comma/whitespace/newline-separated string → a deduped
    list of lowercased, plausible emails (must contain '@')."""
    if isinstance(value, str):
        import re
        parts = re.split(r"[\s,;]+", value)
    else:
        parts = list(value or [])
    out = []
    for p in parts:
        e = (p or "").strip().lower()
        if "@" in e and e not in out:
            out.append(e)
    return out


@transaction.atomic
def create_link(files, *, created_by, name: str = "", require_verify: bool = False,
                allow_download: bool = True, password: str | None = None,
                expires_at=None, download_limit=None, allowed_emails=None) -> ShareLink:
    """Create a link covering `files` (a list/qs of StoredFile)."""
    files = list(files)
    emails = normalize_emails(allowed_emails)
    link = ShareLink.objects.create(
        token=_new_token(),
        name=(name or ShareLink.default_name(len(files)))[:120],
        created_by=created_by,
        require_email_verify=bool(require_verify) or bool(emails),  # whitelist needs verify
        allowed_emails=emails,
        allow_download=bool(allow_download),
        password_hash=make_password(password) if password else "",
        expires_at=expires_at,
        download_limit=download_limit,
    )
    ShareLinkFile.objects.bulk_create(
        [ShareLinkFile(share_link=link, stored_file=f) for f in files]
    )
    sync_file_public_flags(files)

    from apps.audit.models import ActivityAction, ActivityLog

    n = len(files)
    ActivityLog.objects.create(
        actor=created_by, action=ActivityAction.OTHER,
        message=f"Created share link '{link.name}' over {n} file{'s' if n != 1 else ''}",
    )
    return link


@transaction.atomic
def update_link(link: ShareLink, *, name=KEEP, require_verify=KEEP, allow_download=KEEP,
                password=KEEP, expires_at=KEEP, download_limit=KEEP, allowed_emails=KEEP) -> ShareLink:
    """Update a link's settings. `password`: KEEP leaves it, "" clears, else sets."""
    if name is not KEEP:
        link.name = (name or ShareLink.default_name(link.files.count()))[:120]
    if allowed_emails is not KEEP:
        link.allowed_emails = normalize_emails(allowed_emails)
    if require_verify is not KEEP:
        link.require_email_verify = bool(require_verify)
    if allow_download is not KEEP:
        link.allow_download = bool(allow_download)
    if expires_at is not KEEP:
        link.expires_at = expires_at
    if download_limit is not KEEP:
        link.download_limit = download_limit
    if password is not KEEP:
        link.password_hash = make_password(password) if password else ""
    # A whitelist always implies verification.
    if link.allowed_emails:
        link.require_email_verify = True
    link.save()
    sync_file_public_flags(link.files.all())
    return link


@transaction.atomic
def set_link_files(link: ShareLink, files) -> ShareLink:
    """Replace the link's file set."""
    files = list(files)
    old = list(link.files.all())
    link.link_files.all().delete()
    ShareLinkFile.objects.bulk_create(
        [ShareLinkFile(share_link=link, stored_file=f) for f in files]
    )
    sync_file_public_flags(set(old) | set(files))
    return link


@transaction.atomic
def rotate_link(link: ShareLink) -> str:
    """Mint a fresh token (old URL + outstanding verify challenges go stale)."""
    link.token = _new_token()
    link.save(update_fields=["token", "updated_at"])
    sync_file_public_flags(link.files.all())
    return link.token


@transaction.atomic
def disable_link(link: ShareLink) -> None:
    link.is_active = False
    link.save(update_fields=["is_active", "updated_at"])
    sync_file_public_flags(link.files.all())


@transaction.atomic
def enable_link(link: ShareLink) -> None:
    link.is_active = True
    link.save(update_fields=["is_active", "updated_at"])
    sync_file_public_flags(link.files.all())


@transaction.atomic
def delete_link(link: ShareLink) -> None:
    files = list(link.files.all())
    link.delete()  # cascades ShareLinkFile rows
    sync_file_public_flags(files)


def sync_file_public_flags(files) -> None:
    """Recompute the denormalised is_public / public_token hint per file.

    `is_public` = the file is in at least one *live* link; `public_token` = the
    file's newest live link token (best-effort — only used for badges/landing
    fallbacks). The public flow itself never reads these.
    """
    today = timezone.localdate()
    for f in files:
        live = (
            ShareLink.objects.filter(link_files__stored_file=f, is_active=True)
            .filter(Q(expires_at__isnull=True) | Q(expires_at__gte=today))
            .order_by("-created_at")
        )
        link = live.first()
        StoredFile.objects.filter(pk=f.pk).update(
            is_public=bool(link),
            public_token=(link.token if link else ""),
            updated_at=timezone.now(),
        )


def record_view(link: ShareLink, request) -> None:
    """Count a landing view at most once per session, and log it for the
    activity feed (with the verified email when the viewer already has one)."""
    key = f"slview:{link.token}"
    if request.session.get(key):
        return
    request.session[key] = True
    ShareLink.objects.filter(pk=link.pk).update(view_count=F("view_count") + 1)

    from apps.audit.models import ShareLinkView
    from apps.core.utils import get_client_ip

    grant = request.session.get(f"public_grant:{link.token}") or {}
    ShareLinkView.objects.create(
        share_link=link, visitor_email=grant.get("email", ""),
        ip_address=get_client_ip(request),
    )


def reserve_link_download(link: ShareLink) -> bool:
    """Atomically claim one download against the link's total limit (§5.10).

    Returns True if claimed, False if the link is inactive or its limit is hit.
    The check-and-increment is a single UPDATE so concurrent visitors can't
    over-draw the last slot.
    """
    claimed = (
        ShareLink.objects.filter(pk=link.pk, is_active=True)
        .filter(Q(download_limit__isnull=True) | Q(download_count__lt=F("download_limit")))
        .update(download_count=F("download_count") + 1)
    )
    return claimed == 1
