from django.urls import path

from . import views

app_name = "files"

urlpatterns = [
    # Step 1 — transport
    path("upload/", views.upload, name="upload"),
    path("upload/init/", views.init_upload, name="init_upload"),
    path("upload/chunk/", views.upload_chunk, name="upload_chunk"),
    path("upload/status/", views.upload_status, name="upload_status"),
    path("upload/incomplete/", views.incomplete_uploads, name="incomplete_uploads"),
    path("upload/cancel/", views.upload_cancel, name="upload_cancel"),
    path("upload/complete/", views.upload_complete, name="upload_complete"),
    path("upload/finalize/", views.upload_finalize, name="upload_finalize"),
    # Step 2 — metadata + activate
    path("<uuid:uuid>/edit/", views.edit, name="edit"),
    # Lists / detail / download
    path("mine/", views.my_uploads, name="my_uploads"),
    path("assigned/", views.my_files, name="my_files"),
    path("starred/", views.starred, name="starred"),
    path("groups/<int:pk>/", views.group_space, name="group_space"),
    path("zip/", views.download_zip, name="download_zip"),
    path("bulk-delete/", views.bulk_delete, name="bulk_delete"),
    path("bulk-share/", views.bulk_share, name="bulk_share"),
    path("<uuid:uuid>/", views.detail, name="detail"),
    path("<uuid:uuid>/preview/", views.preview, name="preview"),
    path("<uuid:uuid>/thumb/", views.thumb, name="thumb"),
    path("<uuid:uuid>/download/", views.download, name="download"),
    path("<uuid:uuid>/public-link/", views.manage_public_link, name="manage_public_link"),
    path("<uuid:uuid>/share/", views.share, name="share"),
    path("<uuid:uuid>/star/", views.toggle_star, name="toggle_star"),
    path("<uuid:uuid>/delete/", views.delete, name="delete"),
]
