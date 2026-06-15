from django.urls import path

from . import views

app_name = "public"

urlpatterns = [
    path("<str:token>/", views.landing, name="landing"),
    path("<str:token>/password/", views.submit_password, name="submit_password"),
    path("<str:token>/request-code/", views.request_code, name="request_code"),
    path("<str:token>/verify/", views.verify_code, name="verify_code"),
    path("<str:token>/download-all/", views.download_all, name="download_all"),
    path("<str:token>/preview/<uuid:uuid>/", views.preview, name="preview"),
    path("<str:token>/download/<uuid:uuid>/", views.download, name="download"),
]
