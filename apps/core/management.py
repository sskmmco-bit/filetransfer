"""In-app admin console — generic CRUD over the managed models (§ admin).

A small resource registry drives one set of list/create/edit/delete views so the
whole Django-admin surface is available in the application UI, gated to
admin / superadmin users. Site Settings and the Files manager are special-cased.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.accounts.models import Group, Permission, Role
from apps.files.models import Category

from . import management_forms as mf

User = get_user_model()


@dataclass
class Resource:
    key: str
    label: str
    model: type
    form: type | None
    columns: list                      # list of (header, attr)
    search: list = field(default_factory=list)
    superadmin_only: bool = False
    can_create: bool = True
    can_edit: bool = True
    can_delete: bool = True


REGISTRY: dict[str, Resource] = {
    r.key: r for r in [
        Resource("users", "Users", User, mf.UserAdminForm,
                 [("Username", "username"), ("Email", "email"), ("Role", "role"),
                  ("Storage", "storage_label"), ("Active", "is_active")],
                 search=["username", "email", "employee_id", "first_name", "last_name"]),
        Resource("roles", "Roles", Role, mf.RoleAdminForm,
                 [("Name", "name"), ("Slug", "slug"), ("System", "is_system")],
                 search=["name", "slug"], superadmin_only=True),
        Resource("permissions", "Permissions", Permission, None,
                 [("Codename", "codename"), ("Label", "label"), ("Category", "category")],
                 search=["codename", "label"], superadmin_only=True,
                 can_create=False, can_edit=False, can_delete=False),
        Resource("groups", "Groups", Group, mf.GroupAdminForm,
                 [("Name", "name"), ("Description", "description")], search=["name"]),
        Resource("categories", "Categories", Category, mf.CategoryAdminForm,
                 [("Name", "name"), ("Slug", "slug")], search=["name"]),
    ]
}


# ---------------------------------------------------------------------------
def _gate(request, resource: Resource):
    user = request.user
    if not user.is_authenticated or not user.is_admin:
        raise PermissionDenied("Administrator access required.")
    if resource.superadmin_only and not user.is_superadmin:
        raise PermissionDenied("SuperAdmin access required.")


def _cell(obj, attr):
    val = getattr(obj, attr, "")
    if callable(val):
        val = val()
    if isinstance(val, bool):
        return "Yes" if val else "No"
    return "" if val is None else val


def _get_resource(key) -> Resource:
    resource = REGISTRY.get(key)
    if resource is None:
        raise PermissionDenied("Unknown resource.")
    return resource


def manage_list(request, key):
    resource = _get_resource(key)
    _gate(request, resource)
    qs = resource.model.objects.all()
    if not qs.ordered:
        qs = qs.order_by("-pk")
    q = request.GET.get("q", "").strip()
    if q and resource.search:
        cond = Q()
        for f in resource.search:
            cond |= Q(**{f"{f}__icontains": q})
        qs = qs.filter(cond)
    page = Paginator(qs, 25).get_page(request.GET.get("page"))
    rows = [{"pk": o.pk, "cells": [_cell(o, a) for _, a in resource.columns]} for o in page]
    return render(request, "core/console/manage_list.html", {
        "resource": resource, "headers": [h for h, _ in resource.columns],
        "rows": rows, "page": page, "q": q,
    })


def _is_ajax(request) -> bool:
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def manage_edit(request, key, pk=None):
    resource = _get_resource(key)
    _gate(request, resource)
    if (pk is None and not resource.can_create) or (pk is not None and not resource.can_edit):
        raise PermissionDenied("Action not allowed for this resource.")
    instance = get_object_or_404(resource.model, pk=pk) if pk else None
    ajax = _is_ajax(request)
    list_url = reverse("core:manage_list", kwargs={"key": key})

    invalid = False
    if request.method == "POST":
        form = resource.form(request.POST, instance=instance)
        if form.is_valid():
            form.save()
            messages.success(request, f"{resource.label} saved.")
            if ajax:
                return JsonResponse({"ok": True, "redirect": list_url})
            return redirect("core:manage_list", key=key)
        invalid = True
    else:
        form = resource.form(instance=instance)

    # Users / groups / roles get richer, purpose-built forms; the rest are generic.
    rich = {"users": "core/console/_manage_form_user.html",
            "groups": "core/console/_manage_form_group.html",
            "roles": "core/console/_manage_form_role.html"}
    form_template = rich.get(resource.key, "core/console/_manage_form.html")
    ctx = {"resource": resource, "form": form, "is_new": pk is None,
           "form_template": form_template}
    if resource.key == "roles" and "permissions" in form.fields:
        from collections import OrderedDict
        raw = form["permissions"].value() or []
        selected = {str(getattr(x, "pk", x)) for x in raw}
        groups: "OrderedDict[str, list]" = OrderedDict()
        for p in form.fields["permissions"].queryset:
            groups.setdefault(p.category or "Other", []).append({
                "id": p.pk, "codename": p.codename, "label": p.label or p.codename,
                "selected": str(p.pk) in selected})
        ctx["permission_groups"] = [{"category": k, "items": v} for k, v in groups.items()]
        ctx["permission_total"] = sum(len(v) for v in groups.values())
    if resource.key == "groups" and "members" in form.fields:
        raw = form["members"].value() or []
        selected = {str(getattr(x, "pk", x)) for x in raw}
        choices = []
        for u in form.fields["members"].queryset:
            full = (u.get_full_name() or u.username).strip()
            parts = full.split()
            initials = (parts[0][:1] + (parts[1][:1] if len(parts) > 1 else "")).upper()
            choices.append({"id": u.pk, "name": full, "username": u.username,
                            "initials": initials or u.username[:2].upper(),
                            "selected": str(u.pk) in selected})
        ctx["member_choices"] = choices
    if ajax:
        # Form fragment for the modal; 422 signals validation errors to the JS.
        return render(request, form_template, ctx,
                      status=422 if invalid else 200)
    return render(request, "core/console/manage_form.html", ctx)


def manage_delete(request, key, pk):
    resource = _get_resource(key)
    _gate(request, resource)
    if not resource.can_delete:
        raise PermissionDenied("Delete not allowed for this resource.")
    obj = get_object_or_404(resource.model, pk=pk)
    ajax = _is_ajax(request)
    list_url = reverse("core:manage_list", kwargs={"key": key})

    # Guard rails: never delete system roles or your own account.
    if isinstance(obj, Role) and obj.is_system:
        raise PermissionDenied("System roles cannot be deleted.")
    if resource.key == "users" and obj.pk == request.user.pk:
        raise PermissionDenied("You cannot delete your own account.")

    if request.method == "POST":
        obj.delete()
        messages.success(request, f"{resource.label} item deleted.")
        if ajax:
            return JsonResponse({"ok": True, "redirect": list_url})
        return redirect("core:manage_list", key=key)

    ctx = {"resource": resource, "object": obj}
    if ajax:
        return render(request, "core/console/_manage_confirm.html", ctx)
    return render(request, "core/console/manage_confirm_delete.html", ctx)


# ---------------------------------------------------------------------------
# Site Settings (singleton)
# ---------------------------------------------------------------------------
def manage_settings(request):
    from apps.config.admin import SiteSettingsForm
    from apps.config.models import SiteSettings

    if not (request.user.is_authenticated and request.user.is_superadmin):
        raise PermissionDenied("SuperAdmin access required.")
    obj = SiteSettings.get()
    if request.method == "POST":
        form = SiteSettingsForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            messages.success(request, "Site settings saved.")
            return redirect("core:manage_settings")
    else:
        form = SiteSettingsForm(instance=obj)
    return render(request, "core/console/settings.html", {"form": form})


def test_smtp(request):
    """Probe an SMTP server with the posted (or saved) credentials (§5.11).

    Returns JSON {ok, message}. A blank password falls back to the stored one so
    admins can test without re-typing the secret.
    """
    import smtplib

    from apps.config.models import SiteSettings

    if not (request.user.is_authenticated and request.user.is_superadmin):
        raise PermissionDenied("SuperAdmin access required.")
    if request.method != "POST":
        return JsonResponse({"ok": False, "message": "POST required."}, status=405)

    host = (request.POST.get("smtp_host") or "").strip()
    if not host:
        return JsonResponse({"ok": False, "message": "Enter an SMTP host first."})
    try:
        port = int(request.POST.get("smtp_port") or 0) or 587
    except ValueError:
        return JsonResponse({"ok": False, "message": "Port must be a number."})
    username = (request.POST.get("smtp_username") or "").strip()
    password = request.POST.get("smtp_password") or ""
    use_tls = request.POST.get("smtp_use_tls") in ("on", "true", "1")
    use_ssl = request.POST.get("smtp_use_ssl") in ("on", "true", "1")
    if not password:  # blank => reuse the saved secret
        password = SiteSettings.get().get_smtp_password()

    # Optional: actually deliver a test message to this address.
    test_email = (request.POST.get("test_email") or "").strip()
    from_addr = ((request.POST.get("default_from_email") or "").strip()
                 or SiteSettings.get().default_from_email
                 or settings.DEFAULT_FROM_EMAIL)

    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            if use_tls:
                server.starttls()
        try:
            server.ehlo()
            if username:
                server.login(username, password)
            if test_email:
                from email.message import EmailMessage as _Msg
                m = _Msg()
                m["Subject"] = "MMFileTransfer SMTP test"
                m["From"] = from_addr
                m["To"] = test_email
                m.set_content(
                    "This is a test email from MMFileTransfer.\n\n"
                    "If you received this, your SMTP settings are working.")
                server.send_message(m)
        finally:
            server.quit()
    except Exception as exc:  # noqa: BLE001 — surface the reason to the admin
        return JsonResponse({"ok": False, "message": f"{type(exc).__name__}: {exc}"})

    if test_email:
        return JsonResponse({"ok": True,
            "message": f"Test email sent to {test_email} from {from_addr}. "
                       f"Check the inbox (and spam) — delivery can take a moment."})
    return JsonResponse({"ok": True, "message": f"Connected to {host}:{port} successfully."})


# ---------------------------------------------------------------------------
# Files manager (list all + soft-delete)
# ---------------------------------------------------------------------------
def manage_files(request):
    from apps.files.models import FileStatus, StoredFile

    if not (request.user.is_authenticated and request.user.is_admin):
        raise PermissionDenied("Administrator access required.")
    qs = StoredFile.objects.select_related("owner").order_by("-created_at")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(original_filename__icontains=q) | Q(title__icontains=q)
                       | Q(owner__username__icontains=q))
    status = request.GET.get("status", "").strip()
    if status:
        qs = qs.filter(status=status)
    page = Paginator(qs, 25).get_page(request.GET.get("page"))
    return render(request, "core/console/files.html", {
        "page": page, "q": q, "status": status, "statuses": FileStatus.choices,
    })


def manage_file_delete(request, uuid):
    from apps.files import services
    from apps.files.models import FileStatus, StoredFile

    if not (request.user.is_authenticated and request.user.is_admin):
        raise PermissionDenied("Administrator access required.")
    sf = get_object_or_404(StoredFile, uuid=uuid)
    if request.method == "POST" and sf.status not in (FileStatus.DELETED, FileStatus.TRASHED):
        services.soft_delete_stored_file(sf.pk, reason="admin console", actor=request.user)
        messages.success(request, f"'{sf.display_name}' moved to trash.")
    return redirect("core:manage_files")


# ---------------------------------------------------------------------------
# Trash (recoverable deletes) — admin restores or permanently removes
# ---------------------------------------------------------------------------
def manage_trash(request):
    from apps.files.models import FileStatus, StoredFile

    if not (request.user.is_authenticated and request.user.is_admin):
        raise PermissionDenied("Administrator access required.")
    qs = (
        StoredFile.objects.filter(status=FileStatus.TRASHED)
        .select_related("owner", "deleted_by")
        .order_by("-deleted_at")
    )
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(original_filename__icontains=q) | Q(title__icontains=q)
                       | Q(owner__username__icontains=q))
    total_bytes = sum(qs.values_list("size", flat=True))
    page = Paginator(qs, 25).get_page(request.GET.get("page"))
    return render(request, "core/console/trash.html", {
        "page": page, "q": q, "total_bytes": total_bytes, "trash_count": qs.count(),
    })


def manage_trash_restore(request, uuid):
    from apps.files import services
    from apps.files.models import FileStatus, StoredFile

    if not (request.user.is_authenticated and request.user.is_admin):
        raise PermissionDenied("Administrator access required.")
    sf = get_object_or_404(StoredFile, uuid=uuid)
    if request.method == "POST" and sf.status == FileStatus.TRASHED:
        services.restore_stored_file(sf.pk, actor=request.user)
        messages.success(request, f"'{sf.display_name}' restored.")
    return redirect("core:manage_trash")


def manage_trash_purge(request, uuid):
    from apps.files import services
    from apps.files.models import FileStatus, StoredFile

    if not (request.user.is_authenticated and request.user.is_admin):
        raise PermissionDenied("Administrator access required.")
    sf = get_object_or_404(StoredFile, uuid=uuid)
    if request.method == "POST" and sf.status == FileStatus.TRASHED:
        name = sf.display_name
        services.purge_stored_file(sf.pk, reason="admin trash", actor=request.user)
        messages.success(request, f"'{name}' permanently deleted.")
    return redirect("core:manage_trash")


def manage_trash_empty(request):
    """Permanently delete every trashed file in one action."""
    from apps.files import services
    from apps.files.models import FileStatus, StoredFile

    if not (request.user.is_authenticated and request.user.is_admin):
        raise PermissionDenied("Administrator access required.")
    if request.method == "POST":
        ids = list(StoredFile.objects.filter(status=FileStatus.TRASHED).values_list("pk", flat=True))
        for pk in ids:
            services.purge_stored_file(pk, reason="emptied trash", actor=request.user)
        messages.success(request, f"Trash emptied — {len(ids)} file(s) permanently deleted.")
    return redirect("core:manage_trash")
