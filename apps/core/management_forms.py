"""ModelForms for the in-app admin console.

These power the generic CRUD harness so administrators manage everything from
the application UI instead of Django's admin (§ Phase 5/admin).
"""
from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model
from django.utils.text import slugify

from apps.accounts.models import Group, Permission, Role, RolePermission
from apps.config.models import CustomField
from apps.files.models import Category

User = get_user_model()


class UserAdminForm(forms.ModelForm):
    new_password = forms.CharField(
        label="Set / reset password",
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Leave blank to keep the current password (required for new local users).",
    )
    quota_gb = forms.IntegerField(
        label="Storage quota (GB)",
        required=False,
        min_value=0,
        help_text="Per-user storage limit. Leave blank to use the site default; "
                  "enter 0 for unlimited.",
    )

    class Meta:
        model = User
        fields = (
            "username", "email", "employee_id", "first_name", "last_name",
            "role", "auth_source", "is_active", "is_staff",
            "can_upload_public", "must_change_password", "notify_on_assignment",
            "phone", "address", "contact",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Show the current quota (bytes -> GB) on edit.
        inst = self.instance
        if inst and inst.pk and inst.quota_bytes is not None:
            self.fields["quota_gb"].initial = round(inst.quota_bytes / (1024 ** 3))
        if inst and inst.pk:
            self.fields["quota_gb"].help_text += f" · Currently using {inst.storage_label()}."

    def save(self, commit=True):
        user = super().save(commit=False)
        pw = self.cleaned_data.get("new_password")
        if pw:
            user.set_password(pw)
        elif not user.pk:
            # New user with no password — cannot log in until one is set.
            user.set_unusable_password()
        # Quota: blank => inherit default (NULL); a number => that many GB in bytes.
        gb = self.cleaned_data.get("quota_gb")
        user.quota_bytes = None if gb is None else gb * (1024 ** 3)
        if commit:
            user.save()
            self.save_m2m()
        return user


class RoleAdminForm(forms.ModelForm):
    permissions = forms.ModelMultipleChoiceField(
        queryset=Permission.objects.all().order_by("category", "codename"),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    class Meta:
        model = Role
        fields = ("name", "description")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["permissions"].initial = self.instance.permissions.all()

    def save(self, commit=True):
        role = super().save(commit=False)
        if not role.slug:
            role.slug = slugify(role.name)[:32]
        if commit:
            role.save()
            # Sync the RolePermission through-table to the selected permissions.
            selected = set(self.cleaned_data["permissions"].values_list("pk", flat=True))
            RolePermission.objects.filter(role=role).exclude(permission_id__in=selected).delete()
            existing = set(
                RolePermission.objects.filter(role=role).values_list("permission_id", flat=True)
            )
            RolePermission.objects.bulk_create(
                [RolePermission(role=role, permission_id=pid) for pid in selected - existing]
            )
        return role


class GroupAdminForm(forms.ModelForm):
    class Meta:
        model = Group
        fields = ("name", "description", "members")
        widgets = {"members": forms.SelectMultiple(attrs={"size": 8})}


class CategoryAdminForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ("name", "description")


class CustomFieldAdminForm(forms.ModelForm):
    class Meta:
        model = CustomField
        fields = ("key", "label", "field_type", "required", "active", "order")
