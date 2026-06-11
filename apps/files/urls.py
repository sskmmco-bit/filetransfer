from django.urls import path

from . import views

app_name = "files"

urlpatterns = [
    # Step 1 — transport
    path("upload/", views.upload, name="upload"),
    path("upload/init/", views.init_upload, name="init_upload"),
    path("upload/chunk/", views.upload_chunk, name="upload_chunk"),
    path("upload/complete/", views.upload_complete, name="upload_complete"),
    path("upload/finalize/", views.upload_finalize, name="upload_finalize"),
    # Step 2 — metadata + activate
    path("<uuid:uuid>/edit/", views.edit, name="edit"),
    # Lists / detail / download
    path("mine/", views.my_uploads, name="my_uploads"),
    path("assigned/", views.my_files, name="my_files"),
    path("groups/<int:pk>/", views.group_space, name="group_space"),
    path("zip/", views.download_zip, name="download_zip"),
    path("<uuid:uuid>/", views.detail, name="detail"),
    path("<uuid:uuid>/preview/", views.preview, name="preview"),
    path("<uuid:uuid>/download/", views.download, name="download"),
    path("<uuid:uuid>/public-link/", views.manage_public_link, name="manage_public_link"),
    path("<uuid:uuid>/delete/", views.delete, name="delete"),
]
