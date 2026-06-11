from django.contrib import admin
from django.urls import include, path

from apps.core.views import healthz

urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz", healthz, name="healthz"),
    path("accounts/", include("apps.accounts.urls")),
    path("files/", include("apps.files.urls")),
    path("public/", include("apps.public.urls")),
    path("", include("apps.core.urls")),
]
