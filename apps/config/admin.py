from django import forms
from django.contrib import admin

from .models import CustomField, SiteSettings


@admin.register(CustomField)
class CustomFieldAdmin(admin.ModelAdmin):
    list_display = ("label", "key", "field_type", "required", "active", "order")
    list_editable = ("order", "active")
    prepopulated_fields = {"key": ("label",)}


class SiteSettingsForm(forms.ModelForm):
    """Adds a write-only field for the LDAP bind password.

    The encrypted blob is never rendered. Leaving the field blank keeps the
    existing password; typing a value re-encrypts and replaces it.
    """

    ldap_bind_password = forms.CharField(
        label="LDAP bind password",
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Leave blank to keep the current password.",
    )
    smtp_password = forms.CharField(
        label="SMTP password",
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Leave blank to keep the current password.",
    )

    class Meta:
        model = SiteSettings
        exclude = ("ldap_bind_password_encrypted", "smtp_password_encrypted")

    def save(self, commit=True):
        obj = super().save(commit=False)
        if self.cleaned_data.get("ldap_bind_password"):
            obj.set_ldap_bind_password(self.cleaned_data["ldap_bind_password"])
        if self.cleaned_data.get("smtp_password"):
            obj.set_smtp_password(self.cleaned_data["smtp_password"])
        if commit:
            obj.save()
        return obj


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    form = SiteSettingsForm
    list_display = ("site_name", "timezone", "ldap_enabled", "smtp_enabled", "retention_enabled", "updated_at")

    fieldsets = (
        ("General", {"fields": ("site_name", "timezone", "date_format", "pagination_size")}),
        ("Session", {"fields": ("session_idle_timeout_minutes",)}),
        ("Retention", {"fields": ("retention_enabled", "retention_days")}),
        (
            "Email / SMTP",
            {
                "fields": (
                    "smtp_enabled", "smtp_host", "smtp_port", "smtp_username", "smtp_password",
                    "smtp_use_tls", "smtp_use_ssl", "default_from_email", "notification_bcc",
                    "send_welcome_email", "send_assignment_email", "expiry_reminder_days",
                ),
            },
        ),
        (
            "LDAP",
            {
                "fields": (
                    "ldap_enabled",
                    "ldap_server_uri",
                    "ldap_use_ssl",
                    "ldap_connect_timeout",
                    "ldap_bind_dn",
                    "ldap_bind_password",
                    "ldap_base_dn",
                    "ldap_user_search_filter",
                    "ldap_attr_email",
                    "ldap_attr_employee_id",
                    "ldap_attr_first_name",
                    "ldap_attr_last_name",
                ),
            },
        ),
    )

    def has_add_permission(self, request):
        # Singleton — only one row.
        return not SiteSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False
