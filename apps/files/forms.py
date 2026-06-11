"""Upload forms (§5.5).

Step 1 (DirectUploadForm) takes the file itself for the <=50 MiB direct path.
Step 2 (MetadataForm) collects the metadata that activates the file, including
recipient groups (Phase 5) and admin-defined custom fields.
"""
from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model

from apps.accounts.models import Group
from apps.config.models import CustomField

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

    # Build the field name for a custom field.
    CF_PREFIX = "cf_"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._custom_fields = list(CustomField.objects.filter(active=True))
        existing = (self.instance.custom_fields or {}) if self.instance else {}
        for cf in self._custom_fields:
            name = f"{self.CF_PREFIX}{cf.key}"
            initial = existing.get(cf.key)
            if cf.field_type == CustomField.FieldType.NUMBER:
                field = forms.FloatField(required=cf.required, initial=initial)
            elif cf.field_type == CustomField.FieldType.DATE:
                field = forms.DateField(required=cf.required, initial=initial,
                                        widget=forms.DateInput(attrs={"type": "date"}))
            elif cf.field_type == CustomField.FieldType.BOOLEAN:
                field = forms.BooleanField(required=False, initial=bool(initial))
            else:
                field = forms.CharField(required=cf.required, initial=initial)
            field.label = cf.label
            self.fields[name] = field

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("public_require_email_verify") and not cleaned.get("is_public"):
            self.add_error(
                "public_require_email_verify",
                "Email verification only applies to public files.",
            )
        return cleaned

    def custom_field_values(self) -> dict:
        """Collect the custom-field inputs into a JSON-serializable dict."""
        out = {}
        for cf in self._custom_fields:
            val = self.cleaned_data.get(f"{self.CF_PREFIX}{cf.key}")
            if val in (None, ""):
                continue
            out[cf.key] = val.isoformat() if hasattr(val, "isoformat") else val
        return out
