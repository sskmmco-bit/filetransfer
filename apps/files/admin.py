from django.contrib import admin

from . import storage
from .models import Category, FileAssignment, StoredFile, UploadSession


def _purge_blobs_and_log(obj, actor):
    """Remove every MinIO object backing a StoredFile (best-effort) and record
    the permanent deletion in the audit log.

    Django admin deletes the DB row itself; without this the stored object would
    be orphaned in MinIO. We purge first so a delete from the admin behaves like
    the in-app Trash purge (no leaked blobs), and we leave an ActivityLog row
    behind because the cascade will take the file's own download-event history.
    """
    from apps.audit.models import ActivityAction, ActivityLog

    for key in (obj.storage_key, obj.temp_key, obj.thumbnail_key):
        if key:
            try:
                storage.delete_object(key)
            except Exception:  # noqa: BLE001 — a missing blob must not block the delete
                pass
    ActivityLog.objects.create(
        actor=actor if getattr(actor, "pk", None) else None,
        action=ActivityAction.DELETE,
        message=f"Permanently deleted '{obj.display_name}' via Django admin",
    )


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug")
    search_fields = ("name",)
    prepopulated_fields = {"slug": ("name",)}


class FileAssignmentInline(admin.TabularInline):
    model = FileAssignment
    extra = 0
    autocomplete_fields = ("recipient", "assigned_by")


@admin.register(StoredFile)
class StoredFileAdmin(admin.ModelAdmin):
    list_display = ("display_name", "owner", "status", "size", "content_type", "created_at", "uploaded_at")
    list_filter = ("status", "is_public", "is_hidden", "created_at")
    search_fields = ("original_filename", "title", "owner__username", "sha256")
    readonly_fields = ("uuid", "sha256", "size", "content_type", "storage_key", "temp_key", "thumbnail_key", "download_count")
    inlines = (FileAssignmentInline,)
    date_hierarchy = "created_at"

    def delete_model(self, request, obj):
        """Single delete: purge the MinIO object(s) before removing the row."""
        _purge_blobs_and_log(obj, request.user)
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        """Bulk 'delete selected': purge each file's MinIO object(s) first."""
        for obj in queryset:
            _purge_blobs_and_log(obj, request.user)
        super().delete_queryset(request, queryset)

    def get_deleted_objects(self, objs, request):
        """Don't require a separate delete permission on the cascaded audit rows
        (DownloadEvent, etc.). Deleting a file from here is itself the admin
        action; the cascade is expected and the blobs are purged above."""
        deletable, model_count, _perms_needed, protected = super().get_deleted_objects(objs, request)
        return deletable, model_count, set(), protected


@admin.register(UploadSession)
class UploadSessionAdmin(admin.ModelAdmin):
    list_display = ("token", "stored_file", "owner", "is_chunked", "received_size", "expires_at")
    list_filter = ("is_chunked", "expires_at")
    readonly_fields = ("token", "multipart_upload_id", "parts")
