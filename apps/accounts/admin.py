from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import Group, Permission, Role, RolePermission, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ("username", "email", "employee_id", "role", "auth_source", "is_active")
    list_filter = ("role", "auth_source", "is_active", "is_staff")
    search_fields = ("username", "email", "employee_id", "first_name", "last_name")
    autocomplete_fields = ("role",)

    fieldsets = DjangoUserAdmin.fieldsets + (
        (
            "MMFileTransfer",
            {
                "fields": (
                    "employee_id",
                    "auth_source",
                    "role",
                    "phone",
                    "address",
                    "contact",
                    "must_change_password",
                    "notify_on_assignment",
                    "can_upload_public",
                )
            },
        ),
    )


@admin.register(Permission)
class PermissionAdmin(admin.ModelAdmin):
    list_display = ("codename", "label", "category")
    list_filter = ("category",)
    search_fields = ("codename", "label")


class RolePermissionInline(admin.TabularInline):
    model = RolePermission
    extra = 1
    autocomplete_fields = ("permission",)


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_system")
    search_fields = ("name", "slug")
    inlines = (RolePermissionInline,)

    def has_delete_permission(self, request, obj=None):
        # System roles must never be deleted.
        if obj is not None and obj.is_system:
            return False
        return super().has_delete_permission(request, obj)


@admin.register(Group)
class GroupAdmin(admin.ModelAdmin):
    list_display = ("name", "description")
    search_fields = ("name",)
    filter_horizontal = ("members",)
