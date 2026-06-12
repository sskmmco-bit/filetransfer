from django.urls import path

from . import views

app_name = "public"

urlpatterns = [
    path("files/<str:token>/", views.landing, name="landing"),
    path("files/<str:token>/password/", views.submit_password, name="submit_password"),
    path("files/<str:token>/request-code/", views.request_code, name="request_code"),
    path("files/<str:token>/verify/", views.verify_code, name="verify_code"),
    path("files/<str:token>/download-all/", views.download_all, name="download_all"),
    path("files/<str:token>/preview/<uuid:uuid>/", views.preview, name="preview"),
    path("files/<str:token>/download/<uuid:uuid>/", views.download, name="download"),
]
