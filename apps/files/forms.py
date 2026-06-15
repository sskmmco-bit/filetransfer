"""Upload forms (§5.5).

Step 1 (DirectUploadForm) takes the file itself for the <=50 MiB direct path.
Step 2 (MetadataForm) collects the metadata that activates the file, including
recipient groups (Phase 5) and admin-defined custom fields.
"""
from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model

from apps.accounts.models import Group

from .models import Category, StoredFile

User = get_user_model()


class DirectUploadForm(forms.Form):
    file = forms.FileField()


class MetadataForm(forms.ModelForm):
    recipients = forms.ModelMultipleChoiceField(
        queryset=User.objects.filter(is_active=True).order_by("username"),
        required=False,
        widget=forms.SelectMultiple(attrs={"size": 6}),
        help_text="Users this file is assigned to.",
    )
    groups = forms.ModelMultipleChoiceField(
        queryset=Group.objects.all().order_by("name"),
        required=False,
        widget=forms.SelectMultiple(attrs={"size": 4}),
        help_text="All members of the selected groups are assigned the file.",
    )
    categories = forms.ModelMultipleChoiceField(
        queryset=Category.objects.all(),
        required=False,
        widget=forms.SelectMultiple(attrs={"size": 4}),
    )

    class Meta:
        model = StoredFile
        fields = (
            "title",
            "description",
            "categories",
            "is_hidden",
            "expiry_date",
            "download_limit",
            "is_public",
            "public_require_email_verify",
        )
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "expiry_date": forms.DateInput(attrs={"type": "date"}),
        }

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("public_require_email_verify") and not cleaned.get("is_public"):
            self.add_error(
                "public_require_email_verify",
                "Email verification only applies to public files.",
            )
        return cleaned
