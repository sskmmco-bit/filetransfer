from django.contrib import admin

from .models import NotificationLog


@admin.register(NotificationLog)
class NotificationLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "email_type", "recipient", "status", "attempts", "sent_at")
    list_filter = ("status", "email_type", "created_at")
    search_fields = ("recipient", "subject", "idempotency_key")
    readonly_fields = ("idempotency_key", "email_type", "recipient", "subject", "body",
                       "related_id", "attempts", "error", "created_at", "sent_at")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False
