from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from django.core.cache import cache


def audit_view_required(view):
    """Gate the audit console behind the audit.view permission (admins)."""
    @wraps(view)
    @login_required
    def _wrapped(request, *args, **kwargs):
        if not request.user.has_perm_code("audit.view"):
            raise PermissionDenied("Audit access is restricted to administrators.")
        return view(request, *args, **kwargs)

    return _wrapped


def _paginate(request, queryset, per_page=50):
    return Paginator(queryset, per_page).get_page(request.GET.get("page"))


@require_GET
def healthz(request):
    """Liveness/readiness probe — checks DB and cache connectivity."""
    checks = {"database": "ok", "cache": "ok"}
    status = 200

    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        checks["database"] = f"error: {exc.__class__.__name__}"
        status = 503

    try:
        cache.set("healthz", "1", 5)
        if cache.get("healthz") != "1":
            raise RuntimeError("cache round-trip failed")
    except Exception as exc:  # noqa: BLE001
        checks["cache"] = f"error: {exc.__class__.__name__}"
        status = 503

    return JsonResponse({"status": "ok" if status == 200 else "degraded", **checks}, status=status)


def _human_size(num: int) -> str:
    n = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _type_bucket(content_type: str, filename: str) -> str:
    ct = (content_type or "").lower()
    if ct.startswith("image/"):
        return "Images"
    if ct.startswith("video/"):
        return "Videos"
    if ct.startswith("audio/"):
        return "Audio"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in {"zip", "tar", "gz", "rar", "7z", "bz2"}:
        return "Archives"
    return "Documents"


@login_required
def dashboard(request):
    """Modern dashboard with real personal stats (§5.16)."""
    from django.db.models import Count, Q, Sum
    from django.utils import timezone

    from apps.audit.models import ActivityLog, DownloadEvent
    from apps.files.models import FileAssignment, FileStatus, ShareLink, StoredFile

    user = request.user
    owned_active = StoredFile.objects.filter(owner=user, status=FileStatus.ACTIVE)
    assigned = FileAssignment.objects.filter(
        recipient=user, stored_file__status=FileStatus.ACTIVE
    )
    storage_bytes = owned_active.aggregate(s=Sum("size"))["s"] or 0
    today = timezone.localdate()

    # Live share links the user owns (active and not past their expiry date).
    live_q = Q(expires_at__isnull=True) | Q(expires_at__gte=today)
    live_links = ShareLink.objects.filter(created_by=user, is_active=True).filter(live_q)

    quota_eff = user.quota_effective_bytes()  # None => unlimited

    stats = [
        {"label": "My files", "value": owned_active.count(), "icon": "file",
         "sub": f"{StoredFile.objects.filter(owner=user, status=FileStatus.PENDING_METADATA).count()} pending"},
        {"label": "Downloads today", "value": DownloadEvent.objects.filter(
            stored_file__owner=user, created_at__date=today).count(), "icon": "download",
         "sub": "of your files"},
        {"label": "Active shares", "value": live_links.count(),
         "icon": "share", "sub": "live share links"},
        {"label": "Storage used", "value": _human_size(storage_bytes), "icon": "drive",
         "sub": (f"of {_human_size(quota_eff)}" if quota_eff else "unlimited quota")},
    ]

    # Storage breakdown by type (real) — with absolute size per bucket.
    buckets: dict[str, int] = {}
    for f in owned_active.values("content_type", "original_filename", "size"):
        b = _type_bucket(f["content_type"], f["original_filename"])
        buckets[b] = buckets.get(b, 0) + (f["size"] or 0)
    palette = {"Videos": "#6366f1", "Documents": "#06b6d4", "Images": "#10b981",
               "Audio": "#f59e0b", "Archives": "#f97316"}
    total = sum(buckets.values()) or 1
    breakdown = [
        {"label": k, "human": _human_size(v), "pct": round(v / total * 100),
         "color": palette.get(k, "#94a3b8")}
        for k, v in sorted(buckets.items(), key=lambda kv: -kv[1])
    ]

    recent = owned_active.order_by("-uploaded_at")[:6]

    # Recent activity feed — the user's own *file* actions only (uploads,
    # downloads, shares). Auth events (login/logout) and trash/purge management
    # are audit-trail data that belongs in the admin console, not on a regular
    # user's "what's happening with your files" dashboard.
    from apps.audit.models import ActivityAction

    feed_actions = [ActivityAction.UPLOAD, ActivityAction.DOWNLOAD, ActivityAction.OTHER]
    icon_for = {
        ActivityAction.UPLOAD: "upload", ActivityAction.DOWNLOAD: "download",
        ActivityAction.OTHER: "share",
    }
    activity = [
        {"icon": icon_for.get(a.action, "dot"), "action": a.action,
         "message": a.message or a.get_action_display(), "when": a.created_at}
        for a in ActivityLog.objects.filter(actor=user, action__in=feed_actions)
        .order_by("-created_at")[:6]
    ]

    # Active share links — newest live links, with their running download counts.
    share_links = list(
        live_links.annotate(n_files=Count("link_files", distinct=True))
        .order_by("-created_at")[:5]
    )

    context = {
        "stats": stats,
        "breakdown": breakdown,
        "storage_human": _human_size(storage_bytes),
        "quota_human": _human_size(quota_eff) if quota_eff else None,
        "quota_pct": user.quota_pct(),
        "recent_files": recent,
        "activity": activity,
        "share_links": share_links,
        "assigned_count": assigned.count(),
        "can_upload": user.has_perm_code("files.upload"),
        "is_admin": user.has_perm_code("audit.view"),
    }
    return render(request, "core/dashboard.html", context)


# ---------------------------------------------------------------------------
# Audit console (admin-only) — §5.14/§5.15
# ---------------------------------------------------------------------------
@audit_view_required
def console_home(request):
    from django.contrib.auth import get_user_model
    from django.db.models import Count, Q, Sum

    from apps.audit.models import ActivityLog, DownloadEvent, LoginAttempt
    from apps.files.models import FileStatus, StoredFile

    stats = {
        "activity": ActivityLog.objects.count(),
        "logins": LoginAttempt.objects.count(),
        "failed_logins": LoginAttempt.objects.filter(successful=False).count(),
        "downloads": DownloadEvent.objects.count(),
    }

    # ---- System storage (real) ----
    active_files = StoredFile.objects.exclude(status=FileStatus.DELETED)
    total_used = active_files.aggregate(s=Sum("size"))["s"] or 0

    # Breakdown by type bucket.
    buckets: dict[str, int] = {}
    for f in active_files.values("content_type", "original_filename", "size"):
        b = _type_bucket(f["content_type"], f["original_filename"])
        buckets[b] = buckets.get(b, 0) + (f["size"] or 0)
    palette = {"Videos": "#6366f1", "Documents": "#06b6d4", "Images": "#10b981",
               "Audio": "#f59e0b", "Archives": "#f97316"}
    total_bd = sum(buckets.values()) or 1
    breakdown = [
        {"label": k, "human": _human_size(v), "pct": round(v / total_bd * 100),
         "color": palette.get(k, "#94a3b8")}
        for k, v in sorted(buckets.items(), key=lambda kv: -kv[1])
    ]

    # Top storage consumers (top 5 users by owned non-deleted bytes).
    User = get_user_model()
    not_deleted = ~Q(uploaded_files__status=FileStatus.DELETED)
    top_qs = (
        User.objects.annotate(
            used=Sum("uploaded_files__size", filter=not_deleted),
            nfiles=Count("uploaded_files", filter=not_deleted),
        )
        .filter(used__gt=0)
        .order_by("-used")[:5]
    )
    max_used = max((u.used or 0 for u in top_qs), default=1) or 1
    top_consumers = [
        {
            "name": u.get_full_name() or u.username,
            "initials": (u.get_full_name() or u.username)[:2].upper(),
            "role": u.role.name if u.role_id else "—",
            "nfiles": u.nfiles or 0,
            "human": _human_size(u.used or 0),
            "bar_pct": round((u.used or 0) / max_used * 100),
        }
        for u in top_qs
    ]

    storage = {
        "total_human": _human_size(total_used),
        "total_files": active_files.count(),
        "breakdown": breakdown,
        "top_consumers": top_consumers,
    }

    # Counts for the "Manage" cards so each tile is informative at a glance.
    from apps.accounts.models import Group, Role
    from apps.files.models import Category

    counts = {
        "users": User.objects.count(),
        "groups": Group.objects.count(),
        "roles": Role.objects.count(),
        "categories": Category.objects.count(),
        "files": storage["total_files"],
    }

    # Recent activity feed — the latest meaningful actions, newest first.
    recent_activity = list(
        ActivityLog.objects.select_related("actor").order_by("-created_at")[:8]
    )

    return render(request, "core/console/home.html", {
        "stats": stats, "storage": storage,
        "counts": counts, "recent_activity": recent_activity,
    })


@audit_view_required
def console_activity(request):
    from apps.audit.models import ActivityLog

    page = _paginate(request, ActivityLog.objects.select_related("actor"))
    return render(request, "core/console/activity.html", {"page": page})


@audit_view_required
def console_logins(request):
    from apps.audit.models import LoginAttempt

    page = _paginate(request, LoginAttempt.objects.all())
    return render(request, "core/console/logins.html", {"page": page})


@audit_view_required
def console_downloads(request):
    from apps.audit.models import DownloadEvent

    page = _paginate(request, DownloadEvent.objects.select_related("stored_file", "user"))
    return render(request, "core/console/downloads.html", {"page": page})
