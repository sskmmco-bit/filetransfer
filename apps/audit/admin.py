from django.contrib import admin

from .models import ActivityLog, CronLog, DownloadEvent, LoginAttempt


class ReadOnlyAdmin(admin.ModelAdmin):
    """Audit rows are append-only — never editable or addable from the admin."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ActivityLog)
class ActivityLogAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "actor", "action", "ip_address", "message")
    list_filter = ("action", "created_at")
    search_fields = ("message", "ip_address", "actor__username")
    date_hierarchy = "created_at"


@admin.register(LoginAttempt)
class LoginAttemptAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "identifier", "ip_address", "successful")
    list_filter = ("successful", "created_at")
    search_fields = ("identifier", "ip_address")
    date_hierarchy = "created_at"


@admin.register(DownloadEvent)
class DownloadEventAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "stored_file", "user", "visitor_email", "via_public_link", "ip_address")
    list_filter = ("via_public_link", "created_at")
    search_fields = ("visitor_email", "user__username", "stored_file__original_filename")
    date_hierarchy = "created_at"


@admin.register(CronLog)
class CronLogAdmin(ReadOnlyAdmin):
    list_display = ("finished_at", "task_name", "processed_count", "deleted_count")
    list_filter = ("task_name", "finished_at")
    date_hierarchy = "finished_at"
