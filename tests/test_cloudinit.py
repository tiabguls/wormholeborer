import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from wormholeborer import cloudinit


@pytest.fixture
def config() -> dict:
    text = cloudinit.render("tok123", datetime(2026, 11, 25, 19, 30, tzinfo=UTC))
    header, body = text.split("\n", 1)
    assert header == "#cloud-config"
    return json.loads(body)


def files(config: dict) -> dict[str, dict]:
    return {f["path"]: f for f in config["write_files"]}


def test_token_is_root_only(config: dict) -> None:
    header = files(config)["/etc/wormholeborer/auth-header"]
    assert header["content"] == "Authorization: Bearer tok123\n"
    assert header["permissions"] == "0600"


def test_token_appears_only_in_header_file(config: dict) -> None:
    others = [f for p, f in files(config).items() if p != "/etc/wormholeborer/auth-header"]
    assert all("tok123" not in f["content"] for f in others)
    assert all("tok123" not in cmd for cmd in config["runcmd"])


def test_timer_fires_at_expiry_in_utc() -> None:
    eastern = timezone(timedelta(hours=-5))
    text = cloudinit.render("t", datetime(2026, 11, 25, 14, 30, tzinfo=eastern))
    timer = files(json.loads(text.split("\n", 1)[1]))[
        "/etc/systemd/system/wormholeborer-self-destruct.timer"
    ]
    assert "OnCalendar=2026-11-25 19:30:00 UTC" in timer["content"]
    assert "Persistent=true" in timer["content"]


def test_self_destruct_armed_before_install(config: dict) -> None:
    cmds = config["runcmd"]
    arm = cmds.index("systemctl enable --now wormholeborer-self-destruct.timer")
    install = next(i for i, c in enumerate(cmds) if "pip install" in c)
    assert arm < install


def test_install_is_hash_pinned(config: dict) -> None:
    install = next(c for c in config["runcmd"] if "pip install" in c)
    assert "--require-hashes" in install
    assert "--only-binary=:all:" in install
    reqs = files(config)["/etc/wormholeborer/requirements.txt"]["content"]
    pins = [line for line in reqs.splitlines() if not line.startswith("#")]
    assert any(p.startswith("magic-wormhole-mailbox-server==") for p in pins)
    assert any(p.startswith("magic-wormhole-transit-relay==") for p in pins)
    assert all("==" in p and "--hash=sha256:" in p for p in pins)


def test_services_listen_on_expected_ports(config: dict) -> None:
    f = files(config)
    mailbox = f["/etc/systemd/system/wormhole-mailbox.service"]["content"]
    transit = f["/etc/systemd/system/wormhole-transit.service"]["content"]
    assert f"--port=tcp:{cloudinit.MAILBOX_PORT}:interface=\\\\:\\\\:" in mailbox
    assert f"--port=tcp:{cloudinit.TRANSIT_PORT}:interface=\\\\:\\\\:" in transit
    assert "User=wormhole" in mailbox
    assert "User=wormhole" in transit


def test_ssh_password_login_disabled(config: dict) -> None:
    sshd = files(config)["/etc/ssh/sshd_config.d/10-wormholeborer.conf"]["content"]
    assert "PasswordAuthentication no" in sshd


def test_fits_linode_user_data_limit(config: dict) -> None:
    text = "#cloud-config\n" + json.dumps(config, indent=1)
    assert len(text) * 4 / 3 < 65535
