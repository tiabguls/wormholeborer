import base64
import json
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from tests.conftest import FakeLinode
from wormholeborer import ops
from wormholeborer.linode import LinodeClient, LinodeError


def client(fake: FakeLinode) -> LinodeClient:
    return cast("LinodeClient", fake)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10m", timedelta(minutes=10)),
        ("12h", timedelta(hours=12)),
        (" 7d ", timedelta(days=7)),
        ("90m", timedelta(minutes=90)),
    ],
)
def test_parse_ttl(text: str, expected: timedelta) -> None:
    assert ops.parse_ttl(text) == expected


@pytest.mark.parametrize("text", ["", "12", "h", "1.5h", "-1h", "12s", "1h30m"])
def test_parse_ttl_rejects_malformed(text: str) -> None:
    with pytest.raises(ops.OpsError, match="invalid TTL"):
        ops.parse_ttl(text)


@pytest.mark.parametrize("text", ["9m", "0h", "8d", "169h"])
def test_parse_ttl_rejects_out_of_range(text: str) -> None:
    with pytest.raises(ops.OpsError, match="between"):
        ops.parse_ttl(text)


def test_read_secret_file(tmp_path: Path) -> None:
    f = tmp_path / "k"
    f.write_text("  abc\n")
    assert ops.read_secret_file(f, "API token") == "abc"


def test_read_secret_file_missing(tmp_path: Path) -> None:
    with pytest.raises(ops.OpsError, match="API token file not found"):
        ops.read_secret_file(tmp_path / "nope", "API token")


def test_read_secret_file_empty(tmp_path: Path) -> None:
    f = tmp_path / "k"
    f.write_text("\n \n")
    with pytest.raises(ops.OpsError, match="API token file is empty"):
        ops.read_secret_file(f, "API token")


def test_state_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    assert ops.Server.load(path) is None
    server = ops.Server(1, "wormholeborer-x", "203.0.113.7", 2, "us-east", "2026-11-25T19:00:00")
    server.save(path)
    assert ops.Server.load(path) == server


@pytest.mark.parametrize("content", ["not json", '{"linode_id": 1}', "[]"])
def test_state_corrupt(tmp_path: Path, content: str) -> None:
    path = tmp_path / "state.json"
    path.write_text(content)
    with pytest.raises(ops.OpsError, match="corrupt"):
        ops.Server.load(path)


def test_check_region_ok(fake: FakeLinode) -> None:
    ops.check_region(client(fake), "us-east")


@pytest.mark.parametrize("region", ["xx-nometa", "zz-unknown"])
def test_check_region_rejects(fake: FakeLinode, region: str) -> None:
    with pytest.raises(ops.OpsError, match="use one of: us-east, us-ord"):
        ops.check_region(client(fake), region)


def test_check_region_propagates_other_errors(fake: FakeLinode, monkeypatch) -> None:
    def boom(*_a: object) -> None:
        raise LinodeError(500, "down")

    monkeypatch.setattr(fake, "_get_regions", boom)
    with pytest.raises(LinodeError, match="500"):
        ops.check_region(client(fake), "us-east")


def test_create_server(fake: FakeLinode) -> None:
    before = datetime.now(UTC)
    server = ops.create_server(
        client(fake),
        region="us-east",
        linode_type="g6-nanode-1",
        ttl=timedelta(hours=12),
        pubkey="ssh-ed25519 AAAA test",
    )
    inst = fake.instances[server.linode_id]
    token = fake.tokens[server.token_id]
    expires = datetime.fromisoformat(server.expires_at)

    assert server.ipv4 == "203.0.113.7"
    assert timedelta(hours=12) - timedelta(seconds=5) < expires - before <= timedelta(hours=12)
    assert inst["image"] == "linode/debian13"
    assert inst["type"] == "g6-nanode-1"
    assert inst["authorized_keys"] == ["ssh-ed25519 AAAA test"]
    assert inst["label"] == token["label"] == server.label
    assert inst["tags"][0] == "wormholeborer"
    assert ops.expiry_from_tags(inst["tags"]) == expires
    assert token["scopes"] == "linodes:read_write"
    assert token["expiry"] == ops.api_time(expires + ops.TOKEN_GRACE)
    user_data = base64.b64decode(inst["metadata"]["user_data"]).decode()
    assert user_data.startswith("#cloud-config\n")
    assert f"Bearer {token['token']}" in user_data


def test_create_server_revokes_token_when_instance_fails(fake: FakeLinode) -> None:
    fake.fail_instance_create = True
    with pytest.raises(LinodeError, match="sold out"):
        ops.create_server(
            client(fake),
            region="us-east",
            linode_type="g6-nanode-1",
            ttl=timedelta(hours=1),
            pubkey="k",
        )
    assert fake.tokens == {}


def make_server(fake: FakeLinode) -> ops.Server:
    return ops.create_server(
        client(fake),
        region="us-east",
        linode_type="g6-nanode-1",
        ttl=timedelta(hours=1),
        pubkey="k",
    )


def test_destroy(fake: FakeLinode) -> None:
    server = make_server(fake)
    lines = ops.destroy(client(fake), server)
    assert fake.instances == {}
    assert fake.tokens == {}
    assert "deleted" in lines[0]
    assert "revoked" in lines[1]


def test_destroy_after_self_destruct(fake: FakeLinode) -> None:
    server = make_server(fake)
    fake.instances.clear()
    fake.tokens.clear()
    lines = ops.destroy(client(fake), server)
    assert "already gone" in lines[0]
    assert "already expired" in lines[1]


def test_destroy_propagates_other_errors(fake: FakeLinode, monkeypatch) -> None:
    server = make_server(fake)

    def boom(*_a: object) -> None:
        raise LinodeError(403, "forbidden")

    monkeypatch.setattr(fake, "_delete_linode", boom)
    with pytest.raises(LinodeError, match="forbidden"):
        ops.destroy(client(fake), server)


def test_destroy_all_spares_unrelated(fake: FakeLinode) -> None:
    make_server(fake)
    make_server(fake)
    fake.instances[1] = {"id": 1, "label": "prod", "tags": ["prod"]}
    lines = ops.destroy_all(client(fake))
    assert list(fake.instances) == [1]
    assert fake.tokens == {}
    assert len(lines) == 4


def test_destroy_all_nothing(fake: FakeLinode) -> None:
    assert ops.destroy_all(client(fake)) == ["nothing to delete"]


def test_wait_until_running(fake: FakeLinode) -> None:
    server = make_server(fake)
    ops.wait_until_running(client(fake), server.linode_id, timeout=1, sleep=lambda _s: None)


def test_wait_until_running_times_out(fake: FakeLinode, monkeypatch) -> None:
    monkeypatch.setattr(fake, "_get_linode", lambda *_a: {"status": "booting"})
    with pytest.raises(ops.OpsError, match="still 'booting'"):
        ops.wait_until_running(client(fake), 1, timeout=0, sleep=lambda _s: None)


def test_wait_for_ports_open() -> None:
    with socket.create_server(("127.0.0.1", 0)) as srv:
        port = srv.getsockname()[1]
        ops.wait_for_ports("127.0.0.1", [port], timeout=1, sleep=lambda _s: None)


def test_wait_for_ports_times_out() -> None:
    with socket.create_server(("127.0.0.1", 0)) as srv:
        port = srv.getsockname()[1]
    with pytest.raises(ops.OpsError, match="not reachable"):
        ops.wait_for_ports("127.0.0.1", [port], timeout=0, sleep=lambda _s: None)


def test_expiry_from_tags_absent() -> None:
    assert ops.expiry_from_tags(["wormholeborer"]) is None


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(0, 0.0075), (1, 0.0075), (60, 0.0075), (61, 0.015), (60 * 24 * 40, 5.0)],
)
def test_cost_so_far(minutes: int, expected: float) -> None:
    now = datetime(2026, 11, 25, 7, 0, tzinfo=UTC) + timedelta(minutes=minutes)
    assert ops.cost_so_far("2026-11-25T07:00:00", 0.0075, 5.0, now) == pytest.approx(expected)


def test_state_file_is_json(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    ops.Server(1, "l", "ip", 2, "r", "e").save(path)
    assert json.loads(path.read_text())["linode_id"] == 1
