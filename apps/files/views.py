"""Files views (§5.5, §5.8).

Step 1 transport is dual-path:
  * direct  — a single multipart POST for files <= 50 MiB (files:upload).
  * chunked — JSON endpoints files:init_upload -> files:upload_chunk (xN) ->
              files:upload_complete, mapped onto a MinIO multipart upload.
Either way the bytes land in a temp key; complete_transfer() validates and moves
the file to pending_metadata. Step 2 (files:edit) saves metadata and activates.

Download mints a short-lived presigned MinIO URL; full download-limit/public
logic is finished in Phase 3.
"""
from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.core.utils import get_client_ip

from . import services, storage
from .forms import DirectUploadForm, MetadataForm
from .models import FileAssignment, FileStatus, StoredFile, UploadSession

CHUNK_PART_SIZE = services.MIN_PART_SIZE  # client splits large files into >=5 MiB parts


def _require(request, code: str):
    if not request.user.has_perm_code(code):
        raise PermissionDenied(f"Missing permission: {code}")


def _quota_exceeded(user, incoming_bytes: int) -> bool:
    """True if adding `incoming_bytes` would push the user over their quota."""
    remaining = user.quota_remaining_bytes()
    if remaining is None:  # unlimited
        return False
    return incoming_bytes > remaining


PAGE_SIZE = 20


def _paginate(request, queryset, per_page: int = PAGE_SIZE):
    """Return the requested page of a queryset (clamps out-of-range ?page=)."""
    from django.core.paginator import Paginator

    paginator = Paginator(queryset, per_page)
    return paginator.get_page(request.GET.get("page"))


def _new_stored_file(user, filename: str) -> StoredFile:
    sf = StoredFile.objects.create(
        owner=user,
        original_filename=filename[:255],
        status=FileStatus.UPLOADING,
    )
    sf.temp_key = f"temp/{sf.uuid}/{filename}"[:512]
    sf.save(update_fields=["temp_key"])
    return sf


# ---------------------------------------------------------------------------
# Step 1a — direct upload (<= 50 MiB)
# ---------------------------------------------------------------------------
@login_required
def upload(request):
    _require(request, "files.upload")
    if request.method == "POST":
        form = DirectUploadForm(request.POST, request.FILES)
        if form.is_valid():
            upload_file = form.cleaned_data["file"]
            if upload_file.size > services.CHUNK_THRESHOLD:
                return render(
                    request,
                    "files/upload.html",
                    {"form": form, "error": "File exceeds 50 MiB — use the resumable uploader.",
                     "chunk_threshold": services.CHUNK_THRESHOLD},
                    status=413,
                )
            if _quota_exceeded(request.user, upload_file.size):
                return render(
                    request, "files/upload.html",
                    {"form": form, "error": "Storage quota exceeded — this file would put "
                     "you over your limit. Delete some files or ask an admin to raise your quota.",
                     "chunk_threshold": services.CHUNK_THRESHOLD},
                    status=413,
                )
            sf = _new_stored_file(request.user, upload_file.name)
            storage.put_object(sf.temp_key, upload_file, content_type=upload_file.content_type or "application/octet-stream")
            session = UploadSession.objects.create(
                stored_file=sf, owner=request.user, is_chunked=False,
                declared_size=upload_file.size, received_size=upload_file.size,
            )
            try:
                services.complete_transfer(session)
            except ValidationError as exc:
                return render(
                    request, "files/upload.html",
                    {"form": DirectUploadForm(), "error": "; ".join(exc.messages),
                     "chunk_threshold": services.CHUNK_THRESHOLD},
                    status=400,
                )
            return redirect("files:edit", uuid=sf.uuid)
    else:
        form = DirectUploadForm()
    return render(request, "files/upload.html",
                  {"form": form, "chunk_threshold": services.CHUNK_THRESHOLD})


# ---------------------------------------------------------------------------
# Step 1b — chunked (resumable) upload endpoints
# ---------------------------------------------------------------------------
@login_required
@require_POST
def init_upload(request):
    _require(request, "files.upload")
    data = json.loads(request.body or "{}")
    filename = (data.get("filename") or "upload.bin")[:255]
    declared_size = int(data.get("size") or 0)

    if _quota_exceeded(request.user, declared_size):
        remaining = request.user.quota_remaining_bytes()
        from apps.core.views import _human_size
        return JsonResponse(
            {"error": f"Storage quota exceeded — only {_human_size(remaining)} left. "
                      "Delete some files or ask an admin to raise your quota."},
            status=413,
        )

    sf = _new_stored_file(request.user, filename)
    upload_id = storage.create_multipart(sf.temp_key)
    session = UploadSession.objects.create(
        stored_file=sf, owner=request.user, is_chunked=True,
        multipart_upload_id=upload_id, declared_size=declared_size,
    )
    return JsonResponse({"token": str(session.token), "part_size": CHUNK_PART_SIZE})


@login_required
@require_POST
def upload_chunk(request):
    _require(request, "files.upload")
    token = request.POST.get("token")
    session = get_object_or_404(UploadSession, token=token, owner=request.user)
    chunk = request.FILES.get("chunk")
    if chunk is None:
        return JsonResponse({"error": "missing chunk"}, status=400)

    part_number = session.next_part_number()
    etag = storage.upload_part(
        session.stored_file.temp_key, session.multipart_upload_id, part_number, chunk.read()
    )
    session.parts.append({"PartNumber": part_number, "ETag": etag})
    session.received_size += chunk.size
    session.save(update_fields=["parts", "received_size"])
    return JsonResponse({"part_number": part_number, "received": session.received_size})


def _resolve_recipients(tokens):
    """Resolve a list of email/username/group strings to recipients.

    Returns (set_of_user_ids, matched_groups, unresolved_list). A token matching
    a Group name expands to all its current members AND is returned as a group so
    the file can be placed into that group's shared space.
    """
    from django.contrib.auth import get_user_model

    from apps.accounts.models import Group

    User = get_user_model()
    ids, groups, unresolved = set(), [], []
    for raw in tokens:
        token = (raw or "").strip()
        if not token:
            continue
        user = User.objects.filter(is_active=True).filter(
            Q(email__iexact=token) | Q(username__iexact=token)
        ).first()
        if user:
            ids.add(user.pk)
            continue
        group = Group.objects.filter(name__iexact=token).first()
        if group:
            ids.update(group.members.values_list("pk", flat=True))
            groups.append(group)
            continue
        unresolved.append(token)
    return ids, groups, unresolved


@login_required
@require_POST
def upload_finalize(request):
    """Apply shared metadata + recipients to one or more pending files and
    activate them (§5.8). Used by the upload wizard's final step."""
    from datetime import date, timedelta

    from .models import Category

    _require(request, "files.upload")
    data = json.loads(request.body or "{}")

    files = list(StoredFile.objects.filter(
        uuid__in=data.get("file_uuids", []), owner=request.user,
        status=FileStatus.PENDING_METADATA,
    ))
    if not files:
        return JsonResponse({"error": "No uploaded files to finalize."}, status=400)

    recipient_ids, recipient_groups, unresolved = _resolve_recipients(data.get("recipients", []))
    category = Category.objects.filter(pk=data.get("category_id")).first() if data.get("category_id") else None
    expiry_date = None
    if data.get("expiry_date"):
        try:
            expiry_date = date.fromisoformat(data["expiry_date"])
        except ValueError:
            return JsonResponse({"error": "Invalid expiry date."}, status=400)
    elif data.get("expiry_days"):
        expiry_date = timezone.localdate() + timedelta(days=int(data["expiry_days"]))
    make_public = bool(data.get("make_public"))
    download_limit = int(data["download_limit"]) if data.get("download_limit") else None
    notify = bool(data.get("notify", True))

    # A file must have a destination: a recipient, a group, or a public link.
    # Otherwise it would be activated but shared with nobody ("black hole").
    if not recipient_ids and not recipient_groups and not make_public:
        return JsonResponse(
            {"error": "Choose at least one recipient or group, or enable a "
                      "public link — otherwise the file is shared with no one."},
            status=400,
        )

    # All files in one upload share a SINGLE public link (a "transfer bundle"),
    # like WeTransfer — one token covers every file, not one link per file.
    import secrets
    shared_token = secrets.token_urlsafe(32) if make_public else ""
    require_verify = bool(data.get("public_email_verify")) if make_public else False

    for sf in files:
        sf.title = sf.title or sf.original_filename
        sf.description = (data.get("description") or "")[:5000]
        sf.message = (data.get("message") or "")[:2000]
        sf.expiry_date = expiry_date
        sf.download_limit = download_limit
        sf.is_public = make_public
        sf.public_require_email_verify = require_verify
        sf.save(update_fields=["title", "description", "message", "expiry_date",
                               "download_limit", "is_public", "public_require_email_verify", "updated_at"])
        if category:
            sf.categories.add(category)
        if recipient_groups:
            sf.groups.add(*recipient_groups)  # place into each group's shared space
        services.activate_file(sf.pk, recipient_ids=recipient_ids,
                               assigned_by=request.user, notify=notify)

    public_links = []
    if make_public:
        # Stamp the same token on every file (overrides the per-file token
        # activate_file minted), so they resolve as one bundle.
        StoredFile.objects.filter(pk__in=[f.pk for f in files]).update(
            public_token=shared_token, is_public=True)
        bundle_url = request.build_absolute_uri(
            reverse("public:landing", kwargs={"token": shared_token}))
        name = files[0].display_name if len(files) == 1 else f"{len(files)} files"
        public_links = [{"name": name, "url": bundle_url}]

    return JsonResponse({
        "ok": True, "activated": len(files),
        "public_links": public_links, "unresolved": unresolved,
        "redirect": reverse("files:my_uploads"),
    })


@login_required
@require_POST
def upload_complete(request):
    _require(request, "files.upload")
    data = json.loads(request.body or "{}")
    session = get_object_or_404(UploadSession, token=data.get("token"), owner=request.user)
    sf = session.stored_file
    try:
        services.complete_transfer(session)
    except ValidationError as exc:
        return JsonResponse({"error": "; ".join(exc.messages)}, status=400)
    return JsonResponse({
        "file_uuid": str(sf.uuid),
        "redirect": reverse("files:edit", kwargs={"uuid": sf.uuid}),
    })


# ---------------------------------------------------------------------------
# Step 2 — metadata + activate
# ---------------------------------------------------------------------------
@login_required
def edit(request, uuid):
    sf = get_object_or_404(StoredFile, uuid=uuid, owner=request.user)
    if sf.status not in (FileStatus.PENDING_METADATA, FileStatus.ACTIVE):
        raise PermissionDenied("File is not ready for metadata.")

    if request.method == "POST":
        form = MetadataForm(request.POST, instance=sf)
        if form.is_valid():
            if not form.cleaned_data.get("title"):
                sf.title = sf.original_filename
            sf.custom_fields = form.custom_field_values()
            form.save()  # saves scalar fields + categories M2M (+ custom_fields via instance)
            sf.save(update_fields=["custom_fields", "title"])
            # Expand selected groups into individual recipients (Phase 5) and
            # place the file into each group's shared space.
            recipient_ids = {u.pk for u in form.cleaned_data["recipients"]}
            selected_groups = list(form.cleaned_data["groups"])
            for group in selected_groups:
                recipient_ids.update(group.members.values_list("pk", flat=True))
            if selected_groups:
                sf.groups.add(*selected_groups)
            services.activate_file(
                sf.pk,
                recipient_ids=recipient_ids,
                assigned_by=request.user,
            )
            return redirect("files:detail", uuid=sf.uuid)
    else:
        form = MetadataForm(instance=sf, initial={"title": sf.title or sf.original_filename})
    return render(request, "files/edit.html", {"form": form, "file": sf})


# ---------------------------------------------------------------------------
# Lists + detail + download
# ---------------------------------------------------------------------------
@login_required
def my_uploads(request):
    from django.db.models import Count

    qs = (
        StoredFile.objects.filter(owner=request.user)
        .exclude(status=FileStatus.DELETED)
        .prefetch_related("categories")
        .annotate(
            n_assign=Count("assignments", distinct=True),
            n_group=Count("groups", distinct=True),
        )
        .order_by("-created_at")
    )
    page = _paginate(request, qs)
    return render(request, "files/my_uploads.html", {"files": page, "page": page})


@login_required
def my_files(request):
    qs = (
        FileAssignment.objects.filter(
            recipient=request.user, stored_file__status=FileStatus.ACTIVE
        )
        .select_related("stored_file", "assigned_by")
    )
    page = _paginate(request, qs)
    return render(request, "files/my_files.html", {"assignments": page, "page": page})


@login_required
def group_space(request, pk):
    """A group's shared space: every active file placed in the group.

    Visible to current members of the group and to admins (files.view_all).
    """
    from apps.accounts.models import Group

    group = get_object_or_404(Group, pk=pk)
    is_member = group.members.filter(pk=request.user.pk).exists()
    if not (is_member or request.user.has_perm_code("files.view_all")):
        raise PermissionDenied("You are not a member of this group.")

    files_qs = (
        group.files.filter(status=FileStatus.ACTIVE)
        .select_related("owner")
        .prefetch_related("categories")
        .order_by("-created_at")
    )
    page = _paginate(request, files_qs)
    members = group.members.order_by("first_name", "username")
    return render(request, "files/group_space.html", {
        "group": group, "files": page, "page": page, "members": members,
        "is_member": is_member, "file_count": files_qs.count(),
    })


def preview_kind(content_type: str, filename: str) -> str:
    """Which inline preview the browser can render (else 'none')."""
    ct = (content_type or "").lower()
    if ct.startswith("image/"):
        return "image"
    if ct == "application/pdf":
        return "pdf"
    if ct.startswith("video/"):
        return "video"
    if ct.startswith("audio/"):
        return "audio"
    if ct.startswith("text/") or ct in ("application/json", "application/xml", "text/csv"):
        return "text"
    return "none"


@login_required
def detail(request, uuid):
    sf = get_object_or_404(StoredFile, uuid=uuid)
    is_owner = sf.owner_id == request.user.id
    if not _can_access(sf, request.user):
        raise PermissionDenied("You do not have access to this file.")

    kind = preview_kind(sf.content_type, sf.original_filename) if sf.is_active else "none"
    preview_url = ""
    if kind != "none":
        preview_url = storage.presigned_get_url(
            sf.storage_key, download_name=sf.original_filename,
            inline=True, content_type=sf.content_type,
        )
    return render(request, "files/detail.html", {
        "file": sf, "is_owner": is_owner,
        "preview_kind": kind, "preview_url": preview_url,
    })


@login_required
@require_GET
def preview(request, uuid):
    """Authorize, then redirect to an inline presigned URL (open in new tab)."""
    sf = get_object_or_404(StoredFile, uuid=uuid, status=FileStatus.ACTIVE)
    if not _can_access(sf, request.user):
        raise PermissionDenied("You do not have access to this file.")
    url = storage.presigned_get_url(
        sf.storage_key, download_name=sf.original_filename,
        inline=True, content_type=sf.content_type,
    )
    return HttpResponseRedirect(url)


def _can_access(sf, user) -> bool:
    return (
        sf.owner_id == user.id
        or sf.assignments.filter(recipient=user).exists()
        or sf.groups.filter(members=user).exists()  # member of a group this file is in
        or user.has_perm_code("files.view_all")
    )


@login_required
@require_GET
def download(request, uuid):
    """Authorize, reserve a slot, record the event, then redirect to a
    short-lived presigned MinIO URL (§5.4, §5.10)."""
    _require(request, "files.download")
    sf = get_object_or_404(StoredFile, uuid=uuid, status=FileStatus.ACTIVE)
    if not _can_access(sf, request.user):
        raise PermissionDenied("You do not have access to this file.")

    reserved = services.reserve_download_slot(sf.pk)
    if reserved is None:
        raise PermissionDenied("This file is no longer available for download.")

    from apps.audit.models import DownloadEvent

    DownloadEvent.objects.create(
        stored_file=sf, user=request.user, ip_address=get_client_ip(request)
    )
    url = storage.presigned_get_url(sf.storage_key, download_name=sf.original_filename)
    return HttpResponseRedirect(url)


@login_required
@require_POST
def manage_public_link(request, uuid):
    """Owner action: enable, rotate, or disable the public link (§5.4)."""
    sf = get_object_or_404(StoredFile, uuid=uuid, owner=request.user, status=FileStatus.ACTIVE)
    action = request.POST.get("action")
    if action in ("enable", "rotate"):
        sf.public_require_email_verify = request.POST.get("require_verify") == "on"
        sf.save(update_fields=["public_require_email_verify", "updated_at"])
        services.ensure_public_token(sf, rotate=(action == "rotate"))
    elif action == "disable":
        services.disable_public_link(sf)
    return redirect("files:detail", uuid=sf.uuid)


@login_required
@require_POST
def delete(request, uuid):
    """Owner soft-deletes a file (§5.6.2): object removed, row + audit kept."""
    sf = get_object_or_404(StoredFile, uuid=uuid, owner=request.user)
    if sf.status == FileStatus.DELETED:
        raise PermissionDenied("File is already deleted.")
    services.soft_delete_stored_file(sf.pk, reason="manual delete", actor=request.user)
    return redirect("files:my_uploads")


@login_required
@require_POST
def download_zip(request):
    """Stream selected files as a ZIP (§5.4). Reserves a slot per file."""
    import zipfile
    from tempfile import SpooledTemporaryFile

    from django.http import FileResponse

    from apps.audit.models import DownloadEvent

    _require(request, "files.download")
    uuids = request.POST.getlist("uuids")
    files = StoredFile.objects.filter(uuid__in=uuids, status=FileStatus.ACTIVE)
    accessible = [f for f in files if _can_access(f, request.user)]
    if not accessible:
        raise PermissionDenied("No accessible files selected.")

    spool = SpooledTemporaryFile(max_size=64 * 1024 * 1024)
    with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED) as zf:
        for sf in accessible:
            if services.reserve_download_slot(sf.pk) is None:
                continue  # skip expired / limit-reached files
            body = storage.get_object_body(sf.storage_key)
            try:
                zf.writestr(sf.original_filename, body.read())
            finally:
                body.close()
            DownloadEvent.objects.create(
                stored_file=sf, user=request.user, ip_address=get_client_ip(request)
            )
    spool.seek(0)
    return FileResponse(spool, as_attachment=True, filename="mmftp-files.zip")
