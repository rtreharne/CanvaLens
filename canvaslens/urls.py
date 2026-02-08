from django.contrib import admin
from django.urls import path, include
from django.contrib.auth import views as auth_views
from directory import views as directory_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(template_name="registration/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("password/change/", directory_views.account_password_change, name="account_password_change"),
    path("", include("directory.urls")),
]
