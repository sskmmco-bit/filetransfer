"""Authentication views (§4, §5.7.1, §5.9).

Login is guarded by a throttle/hard-block that runs *before* credentials are
checked. Every attempt (success or failure) is recorded as a LoginAttempt, and
successful login/logout writes an ActivityLog entry. Django's LoginView already
rotates the session key on login, giving us session fixation protection.

2FA (the pending_2fa pre-auth marker) is a Phase 6 extension; the hook is noted
but not implemented here.
"""
from __future__ import annotations

from django.contrib.auth.views import LoginView, LogoutView
from django.shortcuts import render

from apps.core.utils import get_client_ip

from . import security
from .forms import IdentifierAuthenticationForm


class MMLoginView(LoginView):
    template_name = "registration/login.html"
    authentication_form = IdentifierAuthenticationForm
    redirect_authenticated_user = True

    def post(self, request, *args, **kwargs):
        identifier = (request.POST.get("username") or "").strip()
        ip = get_client_ip(request)

        # Hard-block check BEFORE any credential processing (§5.9).
        if security.is_blocked(identifier, ip):
            form = self.get_form()
            form.cleaned_data = {}
            form.add_error(None, "Too many attempts. Please try again later.")
            return self.render_to_response(self.get_context_data(form=form))

        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        ip = get_client_ip(self.request)
        ua = self.request.META.get("HTTP_USER_AGENT", "")
        identifier = (self.request.POST.get("username") or "").strip()
        # super().form_valid() calls auth.login() which rotates the session.
        response = super().form_valid(form)
        security.record_attempt(identifier, ip, success=True, user_agent=ua)
        security.log_activity(
            self.request.user, security.ActivityAction.LOGIN, "User logged in", ip, ua
        )
        return response

    def form_invalid(self, form):
        ip = get_client_ip(self.request)
        ua = self.request.META.get("HTTP_USER_AGENT", "")
        identifier = (self.request.POST.get("username") or "").strip()
        security.record_attempt(identifier, ip, success=False, user_agent=ua)
        security.log_activity(
            None, security.ActivityAction.LOGIN_FAILED, f"Failed login for {identifier!r}", ip, ua
        )
        return super().form_invalid(form)


class MMLogoutView(LogoutView):
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            ip = get_client_ip(request)
            ua = request.META.get("HTTP_USER_AGENT", "")
            security.log_activity(
                request.user, security.ActivityAction.LOGOUT, "User logged out", ip, ua
            )
        return super().dispatch(request, *args, **kwargs)
