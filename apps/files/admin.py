from django.contrib import admin

from .models import Category, FileAssignment, StoredFile, UploadSession


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


@admin.register(UploadSession)
class UploadSessionAdmin(admin.ModelAdmin):
    list_display = ("token", "stored_file", "owner", "is_chunked", "received_size", "expires_at")
    list_filter = ("is_chunked", "expires_at")
    readonly_fields = ("token", "multipart_upload_id", "parts")
