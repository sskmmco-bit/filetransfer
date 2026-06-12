"""In-app admin console — generic CRUD over the managed models (§ admin).

A small resource registry drives one set of list/create/edit/delete views so the
whole Django-admin surface is available in the application UI, gated to
admin / superadmin users. Site Settings and the Files manager are special-cased.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.accounts.models import Group, Permission, Role
from apps.config.models import CustomField
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
        Resource("custom-fields", "Custom fields", CustomField, mf.CustomFieldAdminForm,
                 [("Label", "label"), ("Key", "key"), ("Type", "field_type"),
                  ("Required", "required"), ("Active", "active"), ("Order", "order")],
                 search=["label", "key"]),
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

    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=10)
        else:
            server = smtplib.SMTP(host, port, timeout=10)
            if use_tls:
                server.starttls()
        try:
            server.ehlo()
            if username:
                server.login(username, password)
        finally:
            server.quit()
    except Exception as exc:  # noqa: BLE001 — surface the reason to the admin
        return JsonResponse({"ok": False, "message": f"{type(exc).__name__}: {exc}"})
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
    if request.method == "POST" and sf.status != FileStatus.DELETED:
        services.soft_delete_stored_file(sf.pk, reason="admin console", actor=request.user)
        messages.success(request, f"'{sf.display_name}' deleted.")
    return redirect("core:manage_files")
