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

from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from apps.core.utils import get_client_ip
from apps.files import services, storage
from apps.files.models import FileStatus, StoredFile
from apps.notifications.email import send_public_verification_code

from .forms import CodeForm, EmailForm, PasswordForm
from .models import PublicDownloadVerification

GRANT_TTL_SECONDS = 3600           # anonymous grant lifetime (~1h)
CODE_EXPIRY_MINUTES = 15
MAX_CODE_ATTEMPTS = 5


def _bundle(token: str):
    """All active, public files sharing this token (the transfer bundle)."""
    qs = StoredFile.objects.filter(
        public_token=token, is_public=True, status=FileStatus.ACTIVE
    ).order_by("created_at")
    if not qs.exists():
        raise Http404("This link is invalid or has expired.")
    return qs


def _bundle_file(token: str, uuid):
    return get_object_or_404(
        StoredFile, uuid=uuid, public_token=token, is_public=True, status=FileStatus.ACTIVE
    )


def _needs_verify(files) -> bool:
    return any(f.public_require_email_verify for f in files)


def _needs_password(files) -> bool:
    return any(f.public_password_hash for f in files)


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


def _gate(request, token, files):
    """Redirect to the landing gate if a required password/verify isn't met."""
    if _needs_password(files) and not _has_pw_grant(request, token):
        return redirect("public:landing", token=token)
    if _needs_verify(files) and not _has_valid_grant(request, token):
        return redirect("public:landing", token=token)
    return None


def _landing_ctx(request, token, files, **extra):
    ctx = {
        "files": files, "token": token, "rep": files[0],
        "total_size": sum(f.size for f in files),
        "allow_download": files[0].public_allow_download,
    }
    ctx.update(extra)
    return ctx


# ---------------------------------------------------------------------------
@require_http_methods(["GET"])
def landing(request, token):
    files = list(_bundle(token))
    need_pw = _needs_password(files) and not _has_pw_grant(request, token)
    need_verify = _needs_verify(files) and not _has_valid_grant(request, token)
    ready = not need_pw and not need_verify
    extra = {"ready": ready}
    if need_pw:
        extra["gate"] = "password"; extra["password_form"] = PasswordForm()
    elif need_verify:
        extra["gate"] = "verify"; extra["email_form"] = EmailForm()
    return render(request, "public/landing.html", _landing_ctx(request, token, files, **extra))


@require_http_methods(["POST"])
def submit_password(request, token):
    from django.contrib.auth.hashers import check_password
    from django.utils import timezone

    files = list(_bundle(token))
    rep = files[0]
    form = PasswordForm(request.POST)
    ok = (form.is_valid() and rep.public_password_hash
          and check_password(form.cleaned_data["password"], rep.public_password_hash))
    if not ok:
        return render(request, "public/landing.html", _landing_ctx(
            request, token, files, ready=False, gate="password",
            password_form=form, error="Incorrect password."))
    request.session[_pw_key(token)] = {
        "token": token, "expires": timezone.now().timestamp() + GRANT_TTL_SECONDS,
    }
    return redirect("public:landing", token=token)


@require_http_methods(["POST"])
def request_code(request, token):
    files = list(_bundle(token))
    rep = files[0]
    # Password gate (if any) must be cleared before email verification.
    if _needs_password(files) and not _has_pw_grant(request, token):
        return redirect("public:landing", token=token)
    form = EmailForm(request.POST)
    if not form.is_valid():
        return render(request, "public/landing.html", _landing_ctx(
            request, token, files, ready=False, gate="verify", email_form=form))

    email = form.cleaned_data["email"]
    code = f"{secrets.randbelow(1_000_000):06d}"
    # The challenge is bound to the bundle via a representative file + the token.
    PublicDownloadVerification.objects.filter(stored_file=rep, email=email).delete()
    from django.utils import timezone
    PublicDownloadVerification.objects.create(
        stored_file=rep,
        email=email,
        code_hash=PublicDownloadVerification.hash_code(code),
        token_snapshot=token,
        expires_at=timezone.now() + timezone.timedelta(minutes=CODE_EXPIRY_MINUTES),
    )
    label = rep.display_name if len(files) == 1 else f"{len(files)} files"
    send_public_verification_code(email, label, code)
    return render(request, "public/verify.html",
                  {"token": token, "email": email, "code_form": CodeForm()})


@require_http_methods(["POST"])
def verify_code(request, token):
    from django.utils import timezone

    files = list(_bundle(token))
    rep = files[0]
    email = request.POST.get("email", "")
    form = CodeForm(request.POST)
    challenge = (
        PublicDownloadVerification.objects.filter(stored_file=rep, email=email)
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
def preview(request, token, uuid):
    """Inline preview of a bundle file (no download slot consumed).

    Allowed even for preview-only links — previewing is the point.
    """
    files = list(_bundle(token))
    gate = _gate(request, token, files)
    if gate:
        return gate
    sf = _bundle_file(token, uuid)
    return redirect(storage.presigned_get_url(
        sf.storage_key, download_name=sf.original_filename,
        inline=True, content_type=sf.content_type))


@require_http_methods(["GET"])
def download(request, token, uuid):
    """Download a single file from the bundle."""
    files = list(_bundle(token))
    gate = _gate(request, token, files)
    if gate:
        return gate
    if not files[0].public_allow_download:
        return render(request, "public/denied.html",
                      {"file": files[0], "preview_only": True}, status=403)
    sf = _bundle_file(token, uuid)

    if services.reserve_download_slot(sf.pk) is None:
        return render(request, "public/denied.html", {"file": sf}, status=403)

    grant = request.session.get(_grant_key(token)) or {}
    from apps.audit.models import DownloadEvent
    DownloadEvent.objects.create(
        stored_file=sf, user=None, visitor_email=grant.get("email", ""),
        via_public_link=True, ip_address=get_client_ip(request),
    )
    return redirect(storage.presigned_get_url(sf.storage_key, download_name=sf.original_filename))


@require_http_methods(["GET"])
def download_all(request, token):
    """Stream every file in the bundle as a ZIP (reserves a slot per file)."""
    import zipfile
    from tempfile import SpooledTemporaryFile

    from django.http import FileResponse

    files = list(_bundle(token))
    gate = _gate(request, token, files)
    if gate:
        return gate
    if not files[0].public_allow_download:
        return render(request, "public/denied.html",
                      {"file": files[0], "preview_only": True}, status=403)

    grant = request.session.get(_grant_key(token)) or {}
    from apps.audit.models import DownloadEvent

    tmp = SpooledTemporaryFile(max_size=16 * 1024 * 1024)
    added = 0
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for sf in files:
            if services.reserve_download_slot(sf.pk) is None:
                continue  # skip files whose limit is reached
            data = storage.get_object_body(sf.storage_key).read()
            zf.writestr(sf.original_filename, data)
            DownloadEvent.objects.create(
                stored_file=sf, user=None, visitor_email=grant.get("email", ""),
                via_public_link=True, ip_address=get_client_ip(request),
            )
            added += 1
    if not added:
        return render(request, "public/denied.html", {"file": files[0]}, status=403)
    tmp.seek(0)
    return FileResponse(tmp, as_attachment=True, filename="files.zip", content_type="application/zip")
