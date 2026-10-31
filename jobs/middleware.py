from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.urls import reverse


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        # Jazzmin 3.0.5's fixed theme-mode bootstrap. Tests detect package/template drift.
        # All other initialization is served as local external JS, without unsafe-inline/eval.
        admin_script = (
            " 'sha256-NXkd0T5TtFSa4/WcVcscnCfNRACcjmSaW8Yj/udt+Jw='"
            if request.path.startswith("/admin/")
            else ""
        )
        admin_style = " 'unsafe-inline'" if request.path.startswith("/admin/") else ""
        response["Content-Security-Policy"] = (
            f"default-src 'self'; script-src 'self'{admin_script}; style-src 'self'{admin_style}; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        response["Referrer-Policy"] = "same-origin"
        response["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response["Cache-Control"] = "no-store"
        return response


class PrivilegedMFAMiddleware:
    """Staff must enroll and verify MFA before accessing product or admin data."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            request.user.is_authenticated
            and request.user.is_staff
            and not request.user.is_verified()
            and not request.path.startswith(("/account/", "/static/", "/logout/"))
            and request.path not in {"/favicon.ico", "/health/"}
        ):
            # Redirect only document navigation. Background asset/API requests must not
            # GET the setup wizard: its GET handler resets the pending enrollment key.
            if (
                request.method not in {"GET", "HEAD"}
                or request.path.startswith("/api/")
                or request.headers.get("Sec-Fetch-Dest", "document") not in {"document", "iframe"}
            ):
                return HttpResponseForbidden("Verify multi-factor authentication first.")
            return redirect(reverse("two_factor:setup"))
        return self.get_response(request)
