"""Secure public download — email + verification code (§5.4, §6).

A public link is a *transfer bundle*: one token can cover many files (like
WeTransfer). The landing page lists every active file under the token; each can
be downloaded individually, or all at once as a ZIP.

Anonymous flow (no account is ever created):

  landing (GET /public/files/{token}/)
    - no verification required -> show the file list + download buttons
    - verification required    -> show the email form
  request_code (POST email)  -> create/rotate a challenge, email a 6-digit code
  verify_code (POST code)    -> on success write an anonymous session grant
                                bound to the token (~1h TTL)
  download (GET .../download/{uuid}/) -> reserve a slot for that file, record a
                                DownloadEvent, redirect to a presigned URL
  download_all (GET .../download-all/) -> stream every file as a ZIP

Verification proves identity; it does NOT reserve a slot. A visitor can verify
and still be denied if a file's limit was reached in the meantime (§6).
"""
from __future__ import annotations

import secrets
from functools import wraps

from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from apps.core.utils import get_client_ip
from apps.files import sharelinks, storage
from apps.files.models import FileStatus, ShareLink, StoredFile
from apps.notifications.email import send_public_verification_code

from .forms import CodeForm, EmailForm, PasswordForm
from .models import PublicDownloadVerification

GRANT_TTL_SECONDS = 3600           # anonymous grant lifetime (~1h)
CODE_EXPIRY_MINUTES = 15
MAX_CODE_ATTEMPTS = 5


class LinkUnavailable(Exception):
    """A share link that is missing, disabled, expired, or has no active files.

    Raised by the link helpers and turned into a friendly ``public/unavailable``
    page (HTTP 404) by the ``@public_link_view`` decorator — so external
    recipients never see Django's raw debug/404 page.
    """


def public_link_view(view):
    """Render the branded "link unavailable" page on :class:`LinkUnavailable`."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except LinkUnavailable:
            return render(request, "public/unavailable.html", status=404)

    return wrapped


def _link(token: str) -> ShareLink:
    """The live share link for this token (unavailable if missing/disabled/expired)."""
    link = ShareLink.objects.filter(token=token, is_active=True).first()
    if link is None or not link.is_live:
        raise LinkUnavailable("This link is invalid or has expired.")
    return link


def _link_files(link: ShareLink):
    """Active files covered by the link, oldest first."""
    qs = link.files.filter(status=FileStatus.ACTIVE).order_by("created_at")
    if not qs.exists():
        raise LinkUnavailable("This link is invalid or has expired.")
    return qs


def _link_file(link: ShareLink, uuid):
    return get_object_or_404(
        StoredFile, uuid=uuid, share_links=link, status=FileStatus.ACTIVE
    )


def _grant_key(token: str) -> str:
    return f"public_grant:{token}"


def _pw_key(token: str) -> str:
    return f"public_pw:{token}"


def _grant_ok(request, key: str, token: str) -> bool:
    from django.utils import timezone

    grant = request.session.get(key)
    if not grant or grant.get("token") != token:
        return False
    return timezone.now().timestamp() < grant.get("expires", 0)


def _has_valid_grant(request, token: str) -> bool:
    return _grant_ok(request, _grant_key(token), token)


def _has_pw_grant(request, token: str) -> bool:
    return _grant_ok(request, _pw_key(token), token)


def _gate(request, link, token):
    """Redirect to the landing gate if a required password/verify isn't met."""
    if link.has_password and not _has_pw_grant(request, token):
        return redirect("public:landing", token=token)
    if link.require_email_verify and not _has_valid_grant(request, token):
        return redirect("public:landing", token=token)
    return None


def _landing_ctx(request, link, files, **extra):
    files = list(files)
    ctx = {
        "files": files, "token": link.token, "link": link, "rep": files[0],
        "total_size": sum(f.size for f in files),
        "allow_download": link.allow_download,
    }
    ctx.update(extra)
    return ctx


# ---------------------------------------------------------------------------
@require_http_methods(["GET"])
@public_link_view
def landing(request, token):
    link = _link(token)
    files = list(_link_files(link))
    sharelinks.record_view(link, request)
    need_pw = link.has_password and not _has_pw_grant(request, token)
    need_verify = link.require_email_verify and not _has_valid_grant(request, token)
    ready = not need_pw and not need_verify
    extra = {"ready": ready}
    if need_pw:
        extra["gate"] = "password"; extra["password_form"] = PasswordForm()
    elif need_verify:
        extra["gate"] = "verify"; extra["email_form"] = EmailForm()
    return render(request, "public/landing.html", _landing_ctx(request, link, files, **extra))


@require_http_methods(["POST"])
@public_link_view
def submit_password(request, token):
    from django.contrib.auth.hashers import check_password
    from django.utils import timezone

    link = _link(token)
    files = list(_link_files(link))
    form = PasswordForm(request.POST)
    ok = (form.is_valid() and link.has_password
          and check_password(form.cleaned_data["password"], link.password_hash))
    if not ok:
        return render(request, "public/landing.html", _landing_ctx(
            request, link, files, ready=False, gate="password",
            password_form=form, error="Incorrect password."))
    request.session[_pw_key(token)] = {
        "token": token, "expires": timezone.now().timestamp() + GRANT_TTL_SECONDS,
    }
    return redirect("public:landing", token=token)


@require_http_methods(["POST"])
@public_link_view
def request_code(request, token):
    link = _link(token)
    files = list(_link_files(link))
    # Password gate (if any) must be cleared before email verification.
    if link.has_password and not _has_pw_grant(request, token):
        return redirect("public:landing", token=token)
    form = EmailForm(request.POST)
    if not form.is_valid():
        return render(request, "public/landing.html", _landing_ctx(
            request, link, files, ready=False, gate="verify", email_form=form))

    email = form.cleaned_data["email"]
    # Whitelisted (restricted) link: only listed emails may verify.
    if not link.is_email_allowed(email):
        return render(request, "public/landing.html", _landing_ctx(
            request, link, files, ready=False, gate="verify", email_form=EmailForm(),
            error="This email isn’t authorized to open this link. Ask the sender for access."))
    code = f"{secrets.randbelow(1_000_000):06d}"
    # The challenge is bound to the link + token snapshot (rotate invalidates it).
    PublicDownloadVerification.objects.filter(share_link=link, email=email).delete()
    from django.utils import timezone
    PublicDownloadVerification.objects.create(
        share_link=link,
        email=email,
        code_hash=PublicDownloadVerification.hash_code(code),
        token_snapshot=token,
        expires_at=timezone.now() + timezone.timedelta(minutes=CODE_EXPIRY_MINUTES),
    )
    label = files[0].display_name if len(files) == 1 else f"{len(files)} files"
    send_public_verification_code(email, label, code)
    return render(request, "public/verify.html",
                  {"token": token, "email": email, "code_form": CodeForm()})


@require_http_methods(["POST"])
@public_link_view
def verify_code(request, token):
    from django.utils import timezone

    link = _link(token)
    email = request.POST.get("email", "")
    form = CodeForm(request.POST)
    challenge = (
        PublicDownloadVerification.objects.filter(share_link=link, email=email)
        .order_by("-created_at")
        .first()
    )

    def _reject(msg):
        return render(request, "public/verify.html",
                      {"token": token, "email": email, "code_form": form, "error": msg})

    if not form.is_valid() or challenge is None:
        return _reject("Enter the 6-digit code we emailed you.")
    if challenge.attempts >= MAX_CODE_ATTEMPTS:
        return _reject("Too many attempts. Request a new code.")

    challenge.attempts += 1
    challenge.save(update_fields=["attempts"])

    if not challenge.matches(form.cleaned_data["code"], token):
        return _reject("That code is invalid, expired, or the link has changed.")

    request.session[_grant_key(token)] = {
        "email": email, "token": token,
        "expires": timezone.now().timestamp() + GRANT_TTL_SECONDS,
    }
    challenge.delete()
    return redirect("public:landing", token=token)


@require_http_methods(["GET"])
@public_link_view
def preview(request, token, uuid):
    """Inline preview of a link file (no download slot consumed).

    Allowed even for preview-only links — previewing is the point.
    """
    link = _link(token)
    gate = _gate(request, link, token)
    if gate:
        return gate
    sf = _link_file(link, uuid)
    return redirect(storage.presigned_get_url(
        sf.storage_key, download_name=sf.original_filename,
        inline=True, content_type=sf.content_type))


@require_http_methods(["GET"])
@public_link_view
def download(request, token, uuid):
    """Download a single file via the link (counts toward the link's limit)."""
    link = _link(token)
    gate = _gate(request, link, token)
    if gate:
        return gate
    if not link.allow_download:
        return render(request, "public/denied.html",
                      {"link": link, "preview_only": True}, status=403)
    sf = _link_file(link, uuid)

    if not sharelinks.reserve_link_download(link):
        return render(request, "public/denied.html", {"link": link}, status=403)

    grant = request.session.get(_grant_key(token)) or {}
    from apps.audit.models import DownloadEvent
    DownloadEvent.objects.create(
        stored_file=sf, user=None, visitor_email=grant.get("email", ""),
        via_public_link=True, share_link=link, ip_address=get_client_ip(request),
    )
    return redirect(storage.presigned_get_url(sf.storage_key, download_name=sf.original_filename))


@require_http_methods(["GET"])
@public_link_view
def download_all(request, token):
    """Stream every file in the link as a ZIP (each counts toward the limit)."""
    import zipfile
    from tempfile import SpooledTemporaryFile

    from django.http import FileResponse

    link = _link(token)
    files = list(_link_files(link))
    gate = _gate(request, link, token)
    if gate:
        return gate
    if not link.allow_download:
        return render(request, "public/denied.html",
                      {"link": link, "preview_only": True}, status=403)

    grant = request.session.get(_grant_key(token)) or {}
    from apps.audit.models import DownloadEvent

    tmp = SpooledTemporaryFile(max_size=16 * 1024 * 1024)
    added = 0
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for sf in files:
            if not sharelinks.reserve_link_download(link):
                break  # link-level limit exhausted
            data = storage.get_object_body(sf.storage_key).read()
            zf.writestr(sf.original_filename, data)
            DownloadEvent.objects.create(
                stored_file=sf, user=None, visitor_email=grant.get("email", ""),
                via_public_link=True, share_link=link, ip_address=get_client_ip(request),
            )
            added += 1
    if not added:
        return render(request, "public/denied.html", {"link": link}, status=403)
    tmp.seek(0)
    return FileResponse(tmp, as_attachment=True, filename="files.zip", content_type="application/zip")
