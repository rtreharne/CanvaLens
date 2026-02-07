from django.shortcuts import redirect


class StaffAccessMiddleware:
    EXEMPT_PREFIXES = (
        "/login/",
        "/logout/",
        "/admin/",
        "/static/",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path or "/"
        if path.startswith(self.EXEMPT_PREFIXES):
            return self.get_response(request)

        if not request.user.is_authenticated:
            return redirect(f"/login/?next={path}")

        return self.get_response(request)
