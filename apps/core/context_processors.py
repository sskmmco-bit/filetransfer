"""Template context shared across the app shell (nav, upload wizard)."""
from __future__ import annotations


def app_chrome(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    from django.contrib.auth import get_user_model

    from apps.accounts.models import Group
    from apps.files.models import Category

    from .views import _human_size

    # Groups this user belongs to — shown in the sidebar as shared spaces.
    # Available to every authenticated user, regardless of upload permission.
    my_groups = Group.objects.filter(members=request.user).order_by("name")

    # Sidebar storage-usage widget (view-only).
    used = request.user.storage_used_bytes()
    eff = request.user.quota_effective_bytes()
    storage_widget = {
        "used_human": _human_size(used),
        "quota_human": _human_size(eff) if eff else None,  # None => unlimited
        "pct": request.user.quota_pct(),
    }

    if not request.user.has_perm_code("files.upload"):
        return {"can_upload": False, "my_groups": my_groups,
                "storage_widget": storage_widget}

    User = get_user_model()
    return {
        "can_upload": True,
        "my_groups": my_groups,
        "storage_widget": storage_widget,
        "upload_categories": Category.objects.all(),
        # Recipients are existing users; the wizard picks from these (no free-text).
        "upload_users": User.objects.filter(is_active=True)
            .exclude(pk=request.user.pk).order_by("first_name", "username"),
        "upload_groups": Group.objects.all().order_by("name"),
    }
