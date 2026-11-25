import io
import json
import urllib.error
import urllib.request
from email.message import Message
from typing import Any

import pytest

from wormholeborer.linode import LinodeClient, LinodeError


class FakeResponse(io.BytesIO):
    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_a: object, **_k: object) -> None:
        self.close()


def fake_urlopen(monkeypatch: pytest.MonkeyPatch, responses: list[Any]) -> list[Any]:
    seen: list[urllib.request.Request] = []

    def urlopen(req: urllib.request.Request, timeout: float) -> FakeResponse:
        seen.append(req)
        resp = responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return FakeResponse(json.dumps(resp).encode() if resp is not None else b"")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return seen


def http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("u", code, "msg", Message(), io.BytesIO(body))


def test_request_sends_auth_and_body(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = fake_urlopen(monkeypatch, [{"id": 5}])
    assert LinodeClient("tok").request("POST", "/x", {"a": 1}) == {"id": 5}
    req = seen[0]
    assert req.full_url == "https://api.linode.com/v4/x"
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer tok"
    assert req.data == b'{"a": 1}'


def test_empty_response_body(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_urlopen(monkeypatch, [None])
    assert LinodeClient("tok").request("DELETE", "/x") == {}


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b'{"errors": [{"reason": "Invalid Token"}]}', "Invalid Token"),
        (b'{"errors": [{"field": "region", "reason": "bad"}]}', "region: bad"),
        (b"<html>gateway</html>", "<html>gateway</html>"),
    ],
)
def test_http_error(monkeypatch: pytest.MonkeyPatch, body: bytes, message: str) -> None:
    fake_urlopen(monkeypatch, [http_error(401, body)])
    with pytest.raises(LinodeError, match="error 401") as exc:
        LinodeClient("tok").request("GET", "/x")
    assert message in str(exc.value)
    assert exc.value.status == 401


def test_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_urlopen(monkeypatch, [urllib.error.URLError("no route")])
    with pytest.raises(LinodeError, match="cannot reach") as exc:
        LinodeClient("tok").request("GET", "/x")
    assert exc.value.status == 0


def test_get_all_paginates_with_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = fake_urlopen(
        monkeypatch,
        [{"data": [1, 2], "pages": 2}, {"data": [3], "pages": 2}],
    )
    assert LinodeClient("tok").get_all("/things", {"tags": "t"}) == [1, 2, 3]
    assert "page=2" in seen[1].full_url
    assert seen[0].get_header("X-filter") == '{"tags": "t"}'
