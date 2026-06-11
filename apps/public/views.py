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

from .forms import CodeForm, EmailForm
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


def _requires_verify(files) -> bool:
    return any(f.public_require_email_verify for f in files)


def _grant_key(token: str) -> str:
    return f"public_grant:{token}"


def _has_valid_grant(request, token: str) -> bool:
    from django.utils import timezone

    grant = request.session.get(_grant_key(token))
    if not grant or grant.get("token") != token:
        return False
    return timezone.now().timestamp() < grant.get("expires", 0)


# ---------------------------------------------------------------------------
@require_http_methods(["GET"])
def landing(request, token):
    files = list(_bundle(token))
    ready = not _requires_verify(files) or _has_valid_grant(request, token)
    ctx = {
        "files": files, "token": token, "ready": ready,
        "total_size": sum(f.size for f in files),
        "rep": files[0],
    }
    if not ready:
        ctx["email_form"] = EmailForm()
    return render(request, "public/landing.html", ctx)


@require_http_methods(["POST"])
def request_code(request, token):
    files = list(_bundle(token))
    rep = files[0]
    form = EmailForm(request.POST)
    if not form.is_valid():
        return render(request, "public/landing.html", {
            "files": files, "token": token, "ready": False,
            "total_size": sum(f.size for f in files), "rep": rep, "email_form": form,
        })

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


def _guard_download(request, token):
    """Return None if allowed, else a redirect to the landing/verify gate."""
    files = list(_bundle(token))
    if _requires_verify(files) and not _has_valid_grant(request, token):
        return redirect("public:landing", token=token)
    return None


@require_http_methods(["GET"])
def download(request, token, uuid):
    """Download a single file from the bundle."""
    gate = _guard_download(request, token)
    if gate:
        return gate
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

    gate = _guard_download(request, token)
    if gate:
        return gate
    files = list(_bundle(token))

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
