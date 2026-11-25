"""Minimal Linode API v4 client built on the standard library."""

import json
import urllib.error
import urllib.request
from typing import Any

API_URL = "https://api.linode.com/v4"
TIMEOUT_SECONDS = 30
PAGE_SIZE = 500

type Json = dict[str, Any]


class LinodeError(Exception):
    """An API request failed."""

    def __init__(self, status: int, message: str) -> None:
        """Store the HTTP status alongside the API's error text."""
        super().__init__(f"Linode API error {status}: {message}")
        self.status = status


def _error_message(body: bytes) -> str:
    try:
        errors = json.loads(body)["errors"]
        return "; ".join(
            f"{e['field']}: {e['reason']}" if "field" in e else e["reason"] for e in errors
        )
    except (ValueError, KeyError, TypeError):
        return body.decode(errors="replace")[:500]


class LinodeClient:
    """Authenticated access to the Linode API."""

    def __init__(self, token: str, base_url: str = API_URL) -> None:
        """Create a client that sends `token` as a bearer token."""
        self._token = token
        self._base_url = base_url

    def request(
        self,
        method: str,
        path: str,
        body: Json | None = None,
        headers: dict[str, str] | None = None,
    ) -> Json:
        """Send one request and return the decoded JSON response."""
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self._base_url + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "wormholeborer",
                **(headers or {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise LinodeError(e.code, _error_message(e.read())) from None
        except urllib.error.URLError as e:
            raise LinodeError(0, f"cannot reach {self._base_url}: {e.reason}") from None
        return json.loads(raw) if raw else {}

    def get_all(self, path: str, filters: Json | None = None) -> list[Json]:
        """Fetch every item from a paginated collection endpoint."""
        headers = {"X-Filter": json.dumps(filters)} if filters else None
        items: list[Json] = []
        page = 1
        while True:
            resp = self.request("GET", f"{path}?page={page}&page_size={PAGE_SIZE}", None, headers)
            items.extend(resp["data"])
            if page >= resp["pages"]:
                return items
            page += 1
