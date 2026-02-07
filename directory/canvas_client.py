from urllib.parse import parse_qs, urljoin, urlparse

import requests


class CanvasClientError(Exception):
    pass


class CanvasClient:
    def __init__(self, base_url, token, timeout=20):
        self.base_url = base_url.rstrip("/") + "/"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
        )
        self.timeout = timeout

    def _request(self, path, params=None):
        url = urljoin(self.base_url, path.lstrip("/"))
        response = self.session.get(url, params=params or {}, timeout=self.timeout)
        if response.status_code >= 400:
            message = response.text[:400]
            raise CanvasClientError(f"Canvas API error {response.status_code}: {message}")
        return response

    def _paginated_get(self, path, params=None, progress_callback=None):
        url = urljoin(self.base_url, path.lstrip("/"))
        query = params or {}
        items = []
        default_per_page = int((query or {}).get("per_page") or 100)

        while url:
            response = self.session.get(url, params=query, timeout=self.timeout)
            if response.status_code >= 400:
                message = response.text[:400]
                raise CanvasClientError(f"Canvas API error {response.status_code}: {message}")
            page_items = response.json() or []
            items.extend(page_items)
            if progress_callback:
                fetched_count = len(items)
                estimated_total = self._estimate_paged_total(
                    response, fetched_count=fetched_count, default_per_page=default_per_page
                )
                progress_callback(fetched_count, estimated_total)
            next_url = response.links.get("next", {}).get("url")
            url = next_url
            query = None
        return items

    def _estimate_paged_total(self, response, fetched_count, default_per_page=100):
        try:
            default_per_page = int(default_per_page or 100)
        except (TypeError, ValueError):
            default_per_page = 100

        has_next = bool(response.links.get("next", {}).get("url"))
        if not has_next:
            return fetched_count

        last_url = response.links.get("last", {}).get("url")
        if last_url:
            parsed_last = urlparse(last_url)
            parsed_query = parse_qs(parsed_last.query or "")
            try:
                last_page = int((parsed_query.get("page") or [0])[0] or 0)
            except (TypeError, ValueError):
                last_page = 0
            try:
                per_page = int((parsed_query.get("per_page") or [default_per_page])[0] or default_per_page)
            except (TypeError, ValueError):
                per_page = default_per_page
            if last_page > 0 and per_page > 0:
                return max(fetched_count, last_page * per_page)

        return max(fetched_count + default_per_page, fetched_count)

    def validate_token(self):
        response = self._request("/api/v1/users/self")
        return response.json() or {}

    def list_courses(self):
        params = {
            "enrollment_state": "active",
            "state[]": ["available", "completed", "unpublished"],
            "per_page": 100,
            "include[]": ["term"],
        }
        return self._paginated_get("/api/v1/courses", params=params)

    def list_manageable_accounts(self):
        params = {"per_page": 100}
        return self._paginated_get("/api/v1/manageable_accounts", params=params)

    def list_account_courses(self, account_id):
        params = {
            "per_page": 100,
            "include[]": ["term"],
        }
        return self._paginated_get(f"/api/v1/accounts/{account_id}/courses", params=params)

    def list_accounts(self):
        params = {"per_page": 100}
        return self._paginated_get("/api/v1/accounts", params=params)

    def list_course_assignments(self, course_id):
        params = {
            "per_page": 100,
            "include[]": ["submission", "needs_grading_count", "rubric"],
        }
        return self._paginated_get(f"/api/v1/courses/{course_id}/assignments", params=params)

    def list_assignment_submissions(self, course_id, assignment_id, progress_callback=None):
        params = {
            "per_page": 100,
            "include[]": ["user", "rubric_assessment"],
        }
        return self._paginated_get(
            f"/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions",
            params=params,
            progress_callback=progress_callback,
        )

    def list_course_group_categories(self, course_id):
        params = {"per_page": 100}
        return self._paginated_get(f"/api/v1/courses/{course_id}/group_categories", params=params)

    def list_group_category_groups(self, group_category_id):
        params = {"per_page": 100}
        return self._paginated_get(f"/api/v1/group_categories/{group_category_id}/groups", params=params)

    def list_group_users(self, group_id):
        params = {"per_page": 100}
        return self._paginated_get(f"/api/v1/groups/{group_id}/users", params=params)

    def get_user_profile(self, user_id):
        response = self._request(f"/api/v1/users/{user_id}/profile")
        return response.json() or {}
