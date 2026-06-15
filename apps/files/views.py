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

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.http import Http404, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.core.utils import get_client_ip

from . import services, sharelinks, storage
from .forms import DirectUploadForm, MetadataForm
from .models import FileAssignment, FileStatus, ShareLink, StoredFile, UploadSession

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
    """Return the requested page of a queryset (clamps out-of-range ?page=).

    The page size adapts to the viewport: the client measures how many rows/cards
    fit and sends it via ?per_page= or the `files_pp` cookie (clamped 8..200).
    """
    from django.core.paginator import Paginator

    raw = request.GET.get("per_page") or request.COOKIES.get("files_pp")
    try:
        per_page = max(8, min(int(raw), 200))
    except (TypeError, ValueError):
        pass
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

    expected = session.next_part_number()
    # Resume safety: if the client re-sends a part we already stored (e.g. after
    # a reconnect), acknowledge it without uploading a duplicate.
    client_pn = request.POST.get("part_number")
    if client_pn is not None and int(client_pn) < expected:
        return JsonResponse(
            {"part_number": int(client_pn), "received": session.received_size, "duplicate": True}
        )

    etag = storage.upload_part(
        session.stored_file.temp_key, session.multipart_upload_id, expected, chunk.read()
    )
    session.parts.append({"PartNumber": expected, "ETag": etag})
    session.received_size += chunk.size
    session.save(update_fields=["parts", "received_size"])
    return JsonResponse({"part_number": expected, "received": session.received_size})


@login_required
@require_GET
def upload_status(request):
    """Report progress of an in-flight chunked upload so the client can resume
    from where it stopped (after a cancel, reconnect, or page reload)."""
    _require(request, "files.upload")
    session = (
        UploadSession.objects.filter(token=request.GET.get("token"), owner=request.user)
        .select_related("stored_file")
        .first()
    )
    if (
        not session
        or session.is_expired
        or not session.is_chunked
        or session.stored_file.status != FileStatus.UPLOADING
    ):
        return JsonResponse({"resumable": False})
    return JsonResponse({
        "resumable": True,
        "token": str(session.token),
        "received": session.received_size,
        "next_part": session.next_part_number(),
        "part_size": CHUNK_PART_SIZE,
        "declared_size": session.declared_size,
    })


@login_required
@require_GET
def incomplete_uploads(request):
    """List the user's resumable (interrupted) chunked uploads with progress.

    The browser can't re-open a local file on its own, so the UI lists these and
    asks the user to re-pick the matching file; the transfer then continues from
    `received` using the existing token.
    """
    _require(request, "files.upload")
    sessions = (
        UploadSession.objects.filter(
            owner=request.user,
            is_chunked=True,
            stored_file__status=FileStatus.UPLOADING,
            expires_at__gt=timezone.now(),
        )
        .select_related("stored_file")
        .order_by("-created_at")
    )
    items = [
        {
            "token": str(s.token),
            "filename": s.stored_file.original_filename,
            "declared_size": s.declared_size,
            "received": s.received_size,
            "next_part": s.next_part_number(),
            "part_size": CHUNK_PART_SIZE,
        }
        for s in sessions
        if s.received_size > 0  # only sessions with real progress are worth resuming
    ][:20]
    return JsonResponse({"items": items})


@login_required
@require_POST
def upload_cancel(request):
    """Abort an in-flight upload and clean up its multipart + temp object."""
    _require(request, "files.upload")
    data = json.loads(request.body or "{}")
    session = (
        UploadSession.objects.filter(token=data.get("token"), owner=request.user)
        .select_related("stored_file")
        .first()
    )
    if session:
        sf = session.stored_file
        if session.is_chunked and session.multipart_upload_id:
            storage.abort_multipart(sf.temp_key, session.multipart_upload_id)
        storage.delete_object(sf.temp_key)
        session.delete()
        if sf.status == FileStatus.UPLOADING:  # never activated — drop the row
            sf.delete()
    return JsonResponse({"ok": True})


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

    # Recipients are OPTIONAL: with no destination the file simply lands in the
    # owner's My Files (Private) and can be shared later.

    for sf in files:
        sf.title = sf.title or sf.original_filename
        sf.description = (data.get("description") or "")[:5000]
        sf.message = (data.get("message") or "")[:2000]
        sf.expiry_date = expiry_date
        sf.download_limit = download_limit
        sf.save(update_fields=["title", "description", "message", "expiry_date",
                               "download_limit", "updated_at"])
        if category:
            sf.categories.add(category)
        if recipient_groups:
            sf.groups.add(*recipient_groups)  # place into each group's shared space
        services.activate_file(sf.pk, recipient_ids=recipient_ids,
                               assigned_by=request.user, notify=notify)

    public_links = []
    if make_public:
        # One share link covering every file in the upload, with the chosen
        # access/password/preview controls.
        link = sharelinks.create_link(
            files, created_by=request.user,
            require_verify=bool(data.get("public_email_verify")),
            allow_download=not bool(data.get("preview_only")),
            password=(data.get("password") or None),
            expires_at=expiry_date,
            download_limit=download_limit,
        )
        public_links = [{"name": link.name, "url": link.build_url(request)}]

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
    ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"
    detail_url = reverse("files:detail", kwargs={"uuid": sf.uuid})

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
            # Marking the file public on activation mints a share link for it.
            if form.cleaned_data.get("is_public"):
                sharelinks.create_link(
                    [sf], created_by=request.user,
                    require_verify=bool(form.cleaned_data.get("public_require_email_verify")),
                )
            if ajax:
                return JsonResponse({"ok": True, "redirect": detail_url})
            return redirect("files:detail", uuid=sf.uuid)
        if ajax:
            return render(request, "files/_edit_form.html", {"form": form, "file": sf}, status=422)
    else:
        form = MetadataForm(instance=sf, initial={"title": sf.title or sf.original_filename})

    template = "files/_edit_form.html" if ajax else "files/edit.html"
    return render(request, template, {"form": form, "file": sf})


# ---------------------------------------------------------------------------
# Lists + detail + download
# ---------------------------------------------------------------------------
def _view_mode(request):
    """List vs grid view preference: ?view= wins, else cookie, else 'list'."""
    v = request.GET.get("view")
    if v not in ("list", "grid"):
        v = request.COOKIES.get("files_view", "list")
    return v if v in ("list", "grid") else "list"


def _remember_view(request, response):
    """Persist an explicit ?view= choice in a cookie for next time."""
    v = request.GET.get("view")
    if v in ("list", "grid"):
        response.set_cookie("files_view", v, max_age=60 * 60 * 24 * 365, samesite="Lax")
    return response


def _apply_type_filter(request, qs, field: str = "original_filename"):
    """Narrow a queryset to the ?type= bucket (by filename extension)."""
    from .templatetags.file_extras import type_q

    key = (request.GET.get("type") or "").strip()
    q = type_q(key, field)
    if q is not None:
        qs = qs.filter(q)
    return qs, key


def _starred_ids(request) -> set:
    """PKs of files the current user has starred (for the ★ toggle state)."""
    if not request.user.is_authenticated:
        return set()
    return set(request.user.starred_files.values_list("pk", flat=True))


@login_required
def my_uploads(request):
    from django.db.models import Count

    # Hide in-flight/abandoned transfers (UPLOADING) and deleted rows — only
    # real, finished files (PENDING_METADATA drafts + ACTIVE) belong in My Files.
    qs = (
        StoredFile.objects.filter(owner=request.user)
        .exclude(status__in=[FileStatus.DELETED, FileStatus.UPLOADING])
        .prefetch_related("categories")
        .annotate(
            n_assign=Count("assignments", distinct=True),
            n_group=Count("groups", distinct=True),
        )
        .order_by("-created_at")
    )
    qs, current_type = _apply_type_filter(request, qs)
    page = _paginate(request, qs)
    resp = render(request, "files/my_uploads.html",
                  {"files": page, "page": page, "view": _view_mode(request),
                   "current_type": current_type, "starred_ids": _starred_ids(request)})
    return _remember_view(request, resp)


@login_required
def my_files(request):
    qs = (
        FileAssignment.objects.filter(
            recipient=request.user, stored_file__status=FileStatus.ACTIVE
        )
        .select_related("stored_file", "assigned_by")
    )
    qs, current_type = _apply_type_filter(request, qs, field="stored_file__original_filename")
    page = _paginate(request, qs)
    resp = render(request, "files/my_files.html",
                  {"assignments": page, "page": page, "view": _view_mode(request),
                   "current_type": current_type, "starred_ids": _starred_ids(request)})
    return _remember_view(request, resp)


@login_required
def starred(request):
    """Files the current user has starred — across owned + shared + group files."""
    from django.db.models import Count

    base = (
        StoredFile.objects.filter(stars__user=request.user)
        .exclude(status__in=[FileStatus.DELETED, FileStatus.UPLOADING])
        .select_related("owner")
        .prefetch_related("categories")
        .annotate(
            n_assign=Count("assignments", distinct=True),
            n_group=Count("groups", distinct=True),
        )
        .order_by("-stars__created_at")
    )
    # Only show stars the user can still see (access could have been revoked).
    my_group_ids = list(request.user.file_groups.values_list("pk", flat=True))
    visible = Q(owner=request.user) | Q(assignments__recipient=request.user) | Q(is_public=True)
    if my_group_ids:
        visible |= Q(groups__in=my_group_ids)
    if not request.user.has_perm_code("files.view_all"):
        base = base.filter(visible).distinct()
    qs, current_type = _apply_type_filter(request, base)
    page = _paginate(request, qs)
    resp = render(request, "files/starred.html",
                  {"files": page, "page": page, "view": _view_mode(request),
                   "current_type": current_type, "starred_ids": _starred_ids(request)})
    return _remember_view(request, resp)


@login_required
@require_POST
def toggle_star(request, uuid):
    """Star/unstar a file for the current user. Returns {ok, starred}."""
    from .models import StarredFile

    sf = get_object_or_404(StoredFile, uuid=uuid)
    if not _can_access(sf, request.user):
        raise PermissionDenied("You do not have access to this file.")
    existing = StarredFile.objects.filter(user=request.user, stored_file=sf)
    if existing.exists():
        existing.delete()
        starred_now = False
    else:
        StarredFile.objects.create(user=request.user, stored_file=sf)
        starred_now = True
    return JsonResponse({"ok": True, "starred": starred_now})


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
    total_count = files_qs.count()
    files_qs, current_type = _apply_type_filter(request, files_qs)
    page = _paginate(request, files_qs)
    members = group.members.order_by("first_name", "username")
    resp = render(request, "files/group_space.html", {
        "group": group, "files": page, "page": page, "members": members,
        "is_member": is_member, "file_count": total_count,
        "view": _view_mode(request),
        "current_type": current_type, "starred_ids": _starred_ids(request),
    })
    return _remember_view(request, resp)


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

    ctx = {"file": sf, "is_owner": is_owner,
           "preview_kind": kind, "preview_url": preview_url}
    if is_owner and sf.is_active:
        ctx.update(_share_context(sf, request.user))
        # This file's share links (a file can have several), newest first.
        links = list(sf.share_links.order_by("-created_at"))
        for link in links:
            link.abs_url = link.build_url(request)
        ctx["file_links"] = links
    return render(request, "files/detail.html", ctx)


def _initials(name, fallback=""):
    parts = (name or "").split()
    ini = (parts[0][:1] + (parts[1][:1] if len(parts) > 1 else "")) if parts else ""
    return (ini or fallback[:2]).upper()


def _share_context(sf, owner):
    """Picker data + current recipients/groups for the detail-page share modal."""
    from django.contrib.auth import get_user_model

    from apps.accounts.models import Group

    User = get_user_model()
    shared_user_ids = set(sf.assignments.values_list("recipient_id", flat=True))
    shared_group_ids = set(sf.groups.values_list("pk", flat=True))

    users = [{"id": u.pk, "name": (u.get_full_name() or u.username), "username": u.username,
              "initials": _initials(u.get_full_name() or u.username, u.username),
              "shared": u.pk in shared_user_ids}
             for u in User.objects.filter(is_active=True).exclude(pk=owner.pk).order_by("username")]
    groups = [{"id": g.pk, "name": g.name, "initials": _initials(g.name),
               "shared": g.pk in shared_group_ids}
              for g in Group.objects.order_by("name")]

    shared_users = [u for u in users if u["shared"]]
    shared_groups = [g for g in groups if g["shared"]]
    return {"share_users": users, "share_groups": groups,
            "shared_users": shared_users, "shared_groups": shared_groups}


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


@login_required
@require_GET
def thumb(request, uuid):
    """Serve a grid-view thumbnail for a file (images only); 404 otherwise.

    Uses the generated JPEG thumbnail when present, else falls back to the
    original image inline (the browser downscales it). Non-images have no
    thumbnail — the grid shows a type icon instead.
    """
    sf = get_object_or_404(StoredFile, uuid=uuid)
    if sf.status != FileStatus.ACTIVE or not _can_access(sf, request.user):
        raise Http404("No thumbnail.")
    if sf.thumbnail_key:
        return HttpResponseRedirect(storage.presigned_get_url(
            sf.thumbnail_key, download_name="thumb.jpg", inline=True, content_type="image/jpeg"
        ))
    if preview_kind(sf.content_type, sf.original_filename) == "image":
        return HttpResponseRedirect(storage.presigned_get_url(
            sf.storage_key, download_name=sf.original_filename,
            inline=True, content_type=sf.content_type,
        ))
    raise Http404("No thumbnail.")


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


# ---------------------------------------------------------------------------
# Share links (first-class entities) — list, detail, CRUD (§5.4)
# ---------------------------------------------------------------------------
def _parse_link_settings(src):
    """Pull link controls from a dict (JSON body or POST). Returns a kwargs dict
    suitable for sharelinks.create_link/update_link (password uses KEEP sentinel)."""
    from datetime import date

    def truthy(v):
        return v in (True, "true", "on", "1", 1)

    expires_at = None
    raw_exp = src.get("expiry_date")
    if raw_exp:
        try:
            expires_at = date.fromisoformat(raw_exp)
        except (ValueError, TypeError):
            expires_at = None
    raw_limit = src.get("download_limit")
    download_limit = int(raw_limit) if raw_limit not in (None, "", False) else None

    # password: explicit clear flag wins; else a value sets it; else leave (KEEP).
    if truthy(src.get("clear_password")):
        password = ""
    elif src.get("password"):
        password = src["password"]
    else:
        password = sharelinks.KEEP

    return {
        "name": (src.get("name") or "").strip(),
        "require_verify": truthy(src.get("require_verify")) or src.get("access") == "tracked",
        "allow_download": not truthy(src.get("preview_only")),
        "password": password,
        "expires_at": expires_at,
        "download_limit": download_limit,
    }


@login_required
def share_links(request):
    """The Shared Links management page — every link the user created (§5.4)."""
    from django.db.models import Count

    links = (
        ShareLink.objects.filter(created_by=request.user)
        .annotate(n_files=Count("link_files", distinct=True))
        .order_by("-created_at")
    )
    page = _paginate(request, links)
    for link in page:  # absolute URL for copy/open (templates can't pass request)
        link.abs_url = link.build_url(request)
    return render(request, "files/share_links.html", {"links": page, "page": page})


@login_required
def share_link_detail(request, token):
    from apps.audit.models import DownloadEvent, ShareLinkView

    link = get_object_or_404(ShareLink, token=token, created_by=request.user)
    files = link.files.exclude(status=FileStatus.DELETED).order_by("created_at")

    # Combined activity feed: who viewed / downloaded, newest first (§5.4).
    activity = []
    for d in (DownloadEvent.objects.filter(share_link=link)
              .select_related("user", "stored_file")[:100]):
        who = ((d.user.get_full_name() or d.user.username) if d.user
               else (d.visitor_email or "Anonymous visitor"))
        activity.append({"kind": "download", "who": who, "when": d.created_at,
                         "file": d.stored_file.display_name if d.stored_file_id else ""})
    for v in ShareLinkView.objects.filter(share_link=link)[:100]:
        activity.append({"kind": "view", "who": v.visitor_email or "Anonymous visitor",
                         "when": v.created_at, "file": ""})
    activity.sort(key=lambda a: a["when"], reverse=True)
    activity = activity[:60]

    return render(request, "files/share_link_detail.html", {
        "link": link, "files": files, "public_url": link.build_url(request),
        "activity": activity, "preview_url_name": "files:preview",
    })


@login_required
@require_POST
def share_link_create(request):
    """Create a new link over the given files (JSON). Returns the link URL."""
    data = json.loads(request.body or "{}")
    files = list(StoredFile.objects.filter(
        uuid__in=data.get("file_uuids", []), owner=request.user, status=FileStatus.ACTIVE))
    if not files:
        return JsonResponse({"error": "No accessible files selected."}, status=400)
    opts = _parse_link_settings(data)
    # create_link doesn't take the KEEP sentinel — translate to a real value.
    pwd = opts["password"]
    link = sharelinks.create_link(
        files, created_by=request.user, name=opts["name"],
        require_verify=opts["require_verify"], allow_download=opts["allow_download"],
        password=(None if pwd is sharelinks.KEEP else (pwd or None)),
        expires_at=opts["expires_at"], download_limit=opts["download_limit"],
    )
    return JsonResponse({"ok": True, "token": link.token, "url": link.build_url(request),
                         "name": link.name})


@login_required
@require_POST
def share_link_update(request, token):
    link = get_object_or_404(ShareLink, token=token, created_by=request.user)
    src = json.loads(request.body) if request.content_type == "application/json" else request.POST
    opts = _parse_link_settings(src)
    sharelinks.update_link(
        link, name=opts["name"] or sharelinks.KEEP, require_verify=opts["require_verify"],
        allow_download=opts["allow_download"], password=opts["password"],
        expires_at=opts["expires_at"], download_limit=opts["download_limit"],
    )
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"ok": True})
    return redirect("files:share_link_detail", token=link.token)


@login_required
@require_POST
def share_link_rotate(request, token):
    link = get_object_or_404(ShareLink, token=token, created_by=request.user)
    sharelinks.rotate_link(link)
    return redirect("files:share_link_detail", token=link.token)


@login_required
@require_POST
def share_link_toggle(request, token):
    """Disable or enable a link."""
    link = get_object_or_404(ShareLink, token=token, created_by=request.user)
    if link.is_active:
        sharelinks.disable_link(link)
    else:
        sharelinks.enable_link(link)
    nxt = request.POST.get("next")
    if nxt:
        return redirect(nxt)
    return redirect("files:share_link_detail", token=link.token)


@login_required
@require_POST
def share_link_delete(request, token):
    link = get_object_or_404(ShareLink, token=token, created_by=request.user)
    sharelinks.delete_link(link)
    return redirect("files:share_links")


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
def share(request, uuid):
    """Share an active file with more users/groups from the detail page (§5.8)."""
    from apps.accounts.models import Group

    sf = get_object_or_404(StoredFile, uuid=uuid, owner=request.user)
    if sf.status != FileStatus.ACTIVE:
        return JsonResponse({"error": "Only active files can be shared."}, status=400)

    data = json.loads(request.body or "{}")
    try:
        user_ids = {int(x) for x in data.get("user_ids", [])}
        group_ids = {int(x) for x in data.get("group_ids", [])}
    except (TypeError, ValueError):
        return JsonResponse({"error": "Invalid selection."}, status=400)

    groups = list(Group.objects.filter(pk__in=group_ids))
    recipient_ids = set(user_ids)
    for g in groups:  # expand group members into individual recipients
        recipient_ids.update(g.members.values_list("pk", flat=True))
    recipient_ids.discard(request.user.pk)  # never assign the owner to their own file

    if not recipient_ids and not groups:
        return JsonResponse({"error": "Select at least one person or group."}, status=400)

    added = services.add_recipients(
        sf.pk, recipient_ids=recipient_ids, groups=groups,
        assigned_by=request.user, notify=bool(data.get("notify", True)),
    )
    return JsonResponse({"ok": True, "added": added})


@login_required
@require_POST
def bulk_share(request):
    """Share several selected files at once: to people/groups and/or one
    public link (a bundle) covering the whole selection (§5.8)."""
    from datetime import date

    from apps.accounts.models import Group

    data = json.loads(request.body or "{}")
    files = list(StoredFile.objects.filter(
        uuid__in=data.get("uuids", []), owner=request.user, status=FileStatus.ACTIVE))
    if not files:
        return JsonResponse({"error": "No accessible files selected."}, status=400)

    try:
        user_ids = {int(x) for x in data.get("user_ids", [])}
        group_ids = {int(x) for x in data.get("group_ids", [])}
    except (TypeError, ValueError):
        return JsonResponse({"error": "Invalid selection."}, status=400)

    groups = list(Group.objects.filter(pk__in=group_ids))
    recipient_ids = set(user_ids)
    for g in groups:
        recipient_ids.update(g.members.values_list("pk", flat=True))
    recipient_ids.discard(request.user.pk)

    notify = bool(data.get("notify", True))
    added_total = 0
    if recipient_ids or groups:
        for sf in files:
            added_total += services.add_recipients(
                sf.pk, recipient_ids=recipient_ids, groups=groups,
                assigned_by=request.user, notify=notify)

    public_url = None
    if data.get("make_public"):
        expiry_date = None
        if data.get("expiry_date"):
            try:
                expiry_date = date.fromisoformat(data["expiry_date"])
            except ValueError:
                pass
        link = sharelinks.create_link(
            files, created_by=request.user,
            require_verify=bool(data.get("require_verify")),
            allow_download=not bool(data.get("preview_only")),
            password=(data.get("password") or None),
            expires_at=expiry_date,
            download_limit=int(data["download_limit"]) if data.get("download_limit") else None,
        )
        public_url = link.build_url(request)

    if not added_total and not public_url:
        return JsonResponse({"error": "Pick at least one person/group, or enable a public link."}, status=400)
    return JsonResponse({"ok": True, "added": added_total, "public_url": public_url, "files": len(files)})


@login_required
@require_POST
def bulk_delete(request):
    """Owner soft-deletes several of their files at once (§5.6.2)."""
    uuids = request.POST.getlist("uuids")
    qs = (StoredFile.objects.filter(uuid__in=uuids, owner=request.user)
          .exclude(status=FileStatus.DELETED))
    count = 0
    for sf in qs:
        services.soft_delete_stored_file(sf.pk, reason="manual delete (bulk)", actor=request.user)
        count += 1
    if count:
        messages.success(request, f"{count} file{'' if count == 1 else 's'} deleted.")
    else:
        messages.warning(request, "No files were deleted.")
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
