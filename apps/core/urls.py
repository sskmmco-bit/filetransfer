from django.urls import path

from . import management, views

app_name = "core"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),

    # Audit console
    path("console/", views.console_home, name="console_home"),
    path("console/activity/", views.console_activity, name="console_activity"),
    path("console/logins/", views.console_logins, name="console_logins"),
    path("console/downloads/", views.console_downloads, name="console_downloads"),

    # Site settings + files manager
    path("console/settings/", management.manage_settings, name="manage_settings"),
    path("console/files/", management.manage_files, name="manage_files"),
    path("console/files/<uuid:uuid>/delete/", management.manage_file_delete, name="manage_file_delete"),

    # Generic CRUD over the resource registry
    path("console/<slug:key>/", management.manage_list, name="manage_list"),
    path("console/<slug:key>/new/", management.manage_edit, name="manage_new"),
    path("console/<slug:key>/<int:pk>/edit/", management.manage_edit, name="manage_edit"),
    path("console/<slug:key>/<int:pk>/delete/", management.manage_delete, name="manage_delete"),
]
