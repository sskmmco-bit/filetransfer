from django.contrib import admin

from .models import PublicDownloadVerification


@admin.register(PublicDownloadVerification)
class PublicDownloadVerificationAdmin(admin.ModelAdmin):
    list_display = ("stored_file", "email", "attempts", "created_at", "expires_at")
    search_fields = ("email",)
    readonly_fields = ("code_hash", "token_snapshot", "created_at")

    def has_add_permission(self, request):
        return False
