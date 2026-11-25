from datetime import UTC, datetime
from typing import Any

import pytest

from wormholeborer.linode import Json, LinodeError


class FakeLinode:
    """In-memory stand-in for the Linode API, at the client boundary."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.instances: dict[int, Json] = {}
        self.tokens: dict[int, Json] = {}
        self.regions = {
            "us-east": ["Linodes", "Metadata"],
            "xx-nometa": ["Linodes"],
            "us-ord": ["Linodes", "Metadata"],
        }
        self.fail_instance_create = False
        self.next_id = 100

    def _new_id(self) -> int:
        self.next_id += 1
        return self.next_id

    def request(
        self, method: str, path: str, body: Json | None = None, headers: Any = None
    ) -> Json:
        self.calls.append((method, path, body))
        parts = path.strip("/").split("/")
        handler = getattr(self, f"_{method.lower()}_{parts[0]}")
        return handler(parts, body)

    def _get_regions(self, parts: list[str], _body: Any) -> Json:
        if parts[1] not in self.regions:
            raise LinodeError(404, "Not found")
        return {"id": parts[1], "capabilities": self.regions[parts[1]]}

    def _post_profile(self, _parts: list[str], body: Json) -> Json:
        tid = self._new_id()
        self.tokens[tid] = {"id": tid, "label": body["label"], "token": f"secret{tid}", **body}
        return self.tokens[tid]

    def _delete_profile(self, parts: list[str], _body: Any) -> Json:
        if self.tokens.pop(int(parts[2]), None) is None:
            raise LinodeError(404, "Not found")
        return {}

    def _post_linode(self, _parts: list[str], body: Json) -> Json:
        if self.fail_instance_create:
            raise LinodeError(400, "region: sold out")
        iid = self._new_id()
        self.instances[iid] = {
            "id": iid,
            "ipv4": ["203.0.113.7"],
            "status": "provisioning",
            "created": f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%S}",
            **body,
        }
        return self.instances[iid]

    def _get_linode(self, parts: list[str], _body: Any) -> Json:
        if parts[1] == "types":
            return {"price": {"hourly": 0.0075, "monthly": 5.0}}
        inst = self.instances[int(parts[2])]
        inst["status"] = "running"
        return inst

    def _delete_linode(self, parts: list[str], _body: Any) -> Json:
        if self.instances.pop(int(parts[2]), None) is None:
            raise LinodeError(404, "Not found")
        return {}

    def get_all(self, path: str, filters: Json | None = None) -> list[Json]:
        if path == "/regions":
            return [{"id": r, "capabilities": c} for r, c in self.regions.items()]
        if path == "/profile/tokens":
            return [*self.tokens.values(), {"id": 1, "label": "my-other-token"}]
        assert filters == {"tags": "wormholeborer"}
        return [i for i in self.instances.values() if "wormholeborer" in i["tags"]]


@pytest.fixture
def fake() -> FakeLinode:
    return FakeLinode()
