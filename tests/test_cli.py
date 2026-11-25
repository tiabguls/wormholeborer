from pathlib import Path

import pytest

from tests.conftest import FakeLinode
from wormholeborer import cli, ops


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeLinode) -> Path:
    (tmp_path / "akamai.key").write_text("tok\n")
    (tmp_path / "id_ed25519.pub").write_text("ssh-ed25519 AAAA me\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "LinodeClient", lambda _token: fake)
    monkeypatch.setattr(ops, "wait_for_ports", lambda *_a, **_k: None)
    return tmp_path


STATE = ".wormholeborer-state.json"


def test_up_status_down(workdir: Path, fake: FakeLinode, capsys) -> None:
    assert cli.main(["up", "--ttl", "1h"]) == 0
    out = capsys.readouterr().out
    assert "--relay-url ws://203.0.113.7:4000/v1 --transit-helper tcp:203.0.113.7:4001" in out
    assert (workdir / STATE).exists()

    assert cli.main(["status"]) == 0
    assert "~$0.01 so far" in capsys.readouterr().out

    assert cli.main(["down"]) == 0
    assert fake.instances == {}
    assert fake.tokens == {}
    assert not (workdir / STATE).exists()


def test_up_refuses_second_server(workdir: Path, capsys) -> None:
    assert cli.main(["up"]) == 0
    assert cli.main(["up"]) == 1
    assert "already recorded" in capsys.readouterr().err


def test_up_bad_region_creates_nothing(workdir: Path, fake: FakeLinode, capsys) -> None:
    assert cli.main(["up", "--region", "xx-nometa"]) == 1
    assert "lacks Metadata" in capsys.readouterr().err
    assert fake.tokens == {}
    assert fake.instances == {}


def test_up_bad_ttl(workdir: Path, capsys) -> None:
    assert cli.main(["up", "--ttl", "forever"]) == 1
    assert "invalid TTL" in capsys.readouterr().err


def test_missing_token_file(workdir: Path, capsys) -> None:
    assert cli.main(["--token-file", "nope", "status"]) == 1
    assert "API token file not found" in capsys.readouterr().err


def test_down_without_state(workdir: Path, capsys) -> None:
    assert cli.main(["down"]) == 1
    assert "use --all" in capsys.readouterr().err


def test_down_all(workdir: Path, fake: FakeLinode, capsys) -> None:
    assert cli.main(["up"]) == 0
    assert cli.main(["down", "--all"]) == 0
    assert fake.instances == {}
    assert not (workdir / STATE).exists()


def test_status_empty(workdir: Path, capsys) -> None:
    assert cli.main(["status"]) == 0
    assert "no wormholeborer servers" in capsys.readouterr().out


def test_interrupt_during_up_keeps_state(workdir: Path, monkeypatch, capsys) -> None:
    def interrupt(*_a: object, **_k: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(ops, "wait_until_running", interrupt)
    assert cli.main(["up"]) == 130
    assert "wormholeborer down" in capsys.readouterr().err
    assert (workdir / STATE).exists()
