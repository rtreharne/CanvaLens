from urllib.parse import urlencode, urlparse

from django.conf import settings
from django.shortcuts import redirect, render


class StaffAccessMiddleware:
    EXEMPT_PREFIXES = (
        "/login/",
        "/logout/",
        "/admin/",
        "/static/",
        "/embed/auth/",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path or "/"
        if path.startswith(self.EXEMPT_PREFIXES):
            return self.get_response(request)

        if (
            getattr(settings, "REQUIRE_CANVAS_EMBED", True)
            and self._is_navigation_request(request)
            and not self._is_embedded_request(request)
        ):
            return render(
                request,
                "directory/embed_required.html",
                {"canvas_url": getattr(settings, "CANVAS_URL", "https://canvas.liverpool.ac.uk")},
                status=403,
            )

        if not request.user.is_authenticated:
            next_path = request.get_full_path()
            if self._is_embedded_request(request):
                return redirect(f"/embed/auth/start/?{urlencode({'next': next_path})}")
            return redirect(f"/login/?{urlencode({'next': next_path})}")

        profile = getattr(request.user, "canvas_subaccount_profile", None)
        if profile and profile.owner_id:
            from directory.models import CanvasCredential

            owner_credential = CanvasCredential.objects.filter(user_id=profile.owner_id).first()
            if owner_credential and owner_credential.subaccounts_maintenance_mode:
                return render(
                    request,
                    "directory/subaccount_maintenance.html",
                    status=503,
                )

        return self.get_response(request)

    @staticmethod
    def _is_embedded_request(request):
        # Browsers send `Sec-Fetch-Dest: iframe` for navigations inside an iframe.
        sec_fetch_dest = (request.headers.get("Sec-Fetch-Dest") or "").strip().lower()
        if sec_fetch_dest == "iframe":
            return True
        embed_flag = (request.GET.get("embed") or "").strip()
        if embed_flag == "1":
            return True
        referer = request.headers.get("Referer") or ""
        try:
            referer_host = (urlparse(referer).hostname or "").lower()
        except ValueError:
            referer_host = ""
        return referer_host == "canvas.liverpool.ac.uk"

    @staticmethod
    def _is_navigation_request(request):
        sec_fetch_mode = (request.headers.get("Sec-Fetch-Mode") or "").strip().lower()
        if sec_fetch_mode == "navigate":
            return True

        sec_fetch_dest = (request.headers.get("Sec-Fetch-Dest") or "").strip().lower()
        if sec_fetch_dest in {"document", "iframe"}:
            return True

        accept = (request.headers.get("Accept") or "").lower()
        return request.method == "GET" and "text/html" in accept


class FrameAncestorsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        frame_ancestors = getattr(settings, "FRAME_ANCESTORS", None) or []
        if not frame_ancestors:
            return response

        frame_directive = f"frame-ancestors {' '.join(frame_ancestors)}"
        current = response.get("Content-Security-Policy", "")
        if not current:
            response["Content-Security-Policy"] = frame_directive
            return response

        if "frame-ancestors" in current.lower():
            return response

        response["Content-Security-Policy"] = f"{current.rstrip(' ;')}; {frame_directive}"
        return response
