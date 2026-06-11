from django.urls import path

from .views import MMLoginView, MMLogoutView

app_name = "accounts"

urlpatterns = [
    path("login/", MMLoginView.as_view(), name="login"),
    path("logout/", MMLogoutView.as_view(), name="logout"),
]
