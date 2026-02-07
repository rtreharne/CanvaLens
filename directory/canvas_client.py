from urllib.parse import urljoin

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

    def _paginated_get(self, path, params=None):
        url = urljoin(self.base_url, path.lstrip("/"))
        query = params or {}
        items = []

        while url:
            response = self.session.get(url, params=query, timeout=self.timeout)
            if response.status_code >= 400:
                message = response.text[:400]
                raise CanvasClientError(f"Canvas API error {response.status_code}: {message}")
            items.extend(response.json() or [])
            next_url = response.links.get("next", {}).get("url")
            url = next_url
            query = None
        return items

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
            "include[]": ["submission"],
        }
        return self._paginated_get(f"/api/v1/courses/{course_id}/assignments", params=params)

    def list_assignment_submissions(self, course_id, assignment_id):
        params = {
            "per_page": 100,
            "include[]": ["user"],
        }
        return self._paginated_get(
            f"/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions",
            params=params,
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
