"""Server lifecycle operations against the Linode API."""

import base64
import json
import re
import secrets
import socket
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wormholeborer import cloudinit
from wormholeborer.linode import Json, LinodeClient, LinodeError

TAG = "wormholeborer"
EXPIRY_TAG_PREFIX = "wormholeborer-exp-"
EXPIRY_TAG_FORMAT = "%Y%m%d-%H%M%S"
IMAGE = "linode/debian13"
TOKEN_GRACE = timedelta(hours=1)
MIN_TTL = timedelta(minutes=10)
MAX_TTL = timedelta(days=7)
NOT_FOUND = 404

_TTL_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


class OpsError(Exception):
    """A user-facing failure with a message that explains what to do."""


@dataclass(frozen=True)
class Server:
    """What the tool remembers about the server it created."""

    linode_id: int
    label: str
    ipv4: str
    token_id: int
    region: str
    expires_at: str

    def save(self, path: Path) -> None:
        """Write this server's details to the state file."""
        path.write_text(json.dumps(asdict(self), indent=2) + "\n")

    @classmethod
    def load(cls, path: Path) -> "Server | None":
        """Read the state file, or return None when no server is recorded."""
        if not path.exists():
            return None
        try:
            return cls(**json.loads(path.read_text()))
        except (ValueError, TypeError) as e:
            raise OpsError(f"{path} is corrupt ({e}); fix or delete it, then run down --all") from e


def parse_ttl(text: str) -> timedelta:
    """Parse a duration such as "90m", "12h" or "2d"."""
    match = re.fullmatch(r"(\d+)([mhd])", text.strip())
    if not match:
        raise OpsError(f"invalid TTL {text!r}: use a number followed by m, h or d, e.g. 12h")
    ttl = timedelta(**{_TTL_UNITS[match[2]]: int(match[1])})
    if not MIN_TTL <= ttl <= MAX_TTL:
        raise OpsError(f"TTL must be between {MIN_TTL} and {MAX_TTL}")
    return ttl


def read_secret_file(path: Path, what: str) -> str:
    """Return the stripped contents of a required, non-empty file."""
    try:
        text = path.read_text().strip()
    except FileNotFoundError:
        raise OpsError(f"{what} file not found: {path}") from None
    if not text:
        raise OpsError(f"{what} file is empty: {path}")
    return text


def api_time(dt: datetime) -> str:
    """Format a datetime the way the Linode API expects (UTC, no offset)."""
    return f"{dt.astimezone(UTC):%Y-%m-%dT%H:%M:%S}"


def check_region(client: LinodeClient, region: str) -> None:
    """Fail unless `region` exists and supports Metadata (needed for cloud-init)."""
    try:
        info = client.request("GET", f"/regions/{region}")
    except LinodeError as e:
        if e.status != NOT_FOUND:
            raise
        info = {"capabilities": []}
    if "Metadata" not in info["capabilities"]:
        usable = sorted(
            r["id"] for r in client.get_all("/regions") if "Metadata" in r["capabilities"]
        )
        raise OpsError(
            f"region {region!r} unknown or lacks Metadata; use one of: {', '.join(usable)}"
        )


def create_server(
    client: LinodeClient, *, region: str, linode_type: str, ttl: timedelta, pubkey: str
) -> Server:
    """Create a self-destructing wormhole server and its scoped self-destruct token."""
    now = datetime.now(UTC)
    expires = now + ttl
    label = f"{TAG}-{now:%Y%m%d-%H%M%S}"
    token = client.request(
        "POST",
        "/profile/tokens",
        {"label": label, "scopes": "linodes:read_write", "expiry": api_time(expires + TOKEN_GRACE)},
    )
    user_data = cloudinit.render(token["token"], expires)
    try:
        instance = client.request(
            "POST",
            "/linode/instances",
            {
                "label": label,
                "region": region,
                "type": linode_type,
                "image": IMAGE,
                "root_pass": secrets.token_urlsafe(32),
                "authorized_keys": [pubkey],
                "tags": [TAG, f"{EXPIRY_TAG_PREFIX}{expires:{EXPIRY_TAG_FORMAT}}"],
                "metadata": {"user_data": base64.b64encode(user_data.encode()).decode()},
                "booted": True,
            },
        )
    except LinodeError:
        _delete_ignoring_missing(client, f"/profile/tokens/{token['id']}")
        raise
    return Server(
        linode_id=instance["id"],
        label=label,
        ipv4=instance["ipv4"][0],
        token_id=token["id"],
        region=region,
        expires_at=expires.isoformat(timespec="seconds"),
    )


def wait_until_running(
    client: LinodeClient,
    linode_id: int,
    timeout: float,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Poll until the instance reports status "running"."""
    deadline = time.monotonic() + timeout
    while True:
        status = client.request("GET", f"/linode/instances/{linode_id}")["status"]
        if status == "running":
            return
        if time.monotonic() > deadline:
            raise OpsError(f"instance still {status!r} after {timeout:.0f}s")
        sleep(5)


def wait_for_ports(
    host: str, ports: list[int], timeout: float, sleep: Callable[[float], None] = time.sleep
) -> None:
    """Wait until every port on `host` accepts TCP connections."""
    deadline = time.monotonic() + timeout
    for port in ports:
        while True:
            try:
                socket.create_connection((host, port), timeout=5).close()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise OpsError(f"{host}:{port} not reachable after {timeout:.0f}s") from None
                sleep(5)


def _delete_ignoring_missing(client: LinodeClient, path: str) -> bool:
    """Delete a resource; return False if it was already gone."""
    try:
        client.request("DELETE", path)
    except LinodeError as e:
        if e.status != NOT_FOUND:
            raise
        return False
    return True


def destroy(client: LinodeClient, server: Server) -> list[str]:
    """Delete the server and revoke its token. Returns a line per action taken."""
    gone = _delete_ignoring_missing(client, f"/linode/instances/{server.linode_id}")
    revoked = _delete_ignoring_missing(client, f"/profile/tokens/{server.token_id}")
    return [
        f"linode {server.linode_id} ({server.label}): {'deleted' if gone else 'already gone'}",
        f"token {server.token_id}: {'revoked' if revoked else 'already expired or revoked'}",
    ]


def destroy_all(client: LinodeClient) -> list[str]:
    """Delete every tagged instance and revoke every token this tool created."""
    lines = []
    for inst in list_servers(client):
        _delete_ignoring_missing(client, f"/linode/instances/{inst['id']}")
        lines.append(f"linode {inst['id']} ({inst['label']}): deleted")
    for tok in client.get_all("/profile/tokens"):
        if tok["label"].startswith(f"{TAG}-"):
            _delete_ignoring_missing(client, f"/profile/tokens/{tok['id']}")
            lines.append(f"token {tok['id']} ({tok['label']}): revoked")
    return lines or ["nothing to delete"]


def list_servers(client: LinodeClient) -> list[Json]:
    """Return every instance carrying the tool's tag."""
    return client.get_all("/linode/instances", {"tags": TAG})


def expiry_from_tags(tags: list[str]) -> datetime | None:
    """Recover the self-destruct time recorded in an instance's tags."""
    for tag in tags:
        if tag.startswith(EXPIRY_TAG_PREFIX):
            stamp = tag.removeprefix(EXPIRY_TAG_PREFIX)
            return datetime.strptime(stamp, EXPIRY_TAG_FORMAT).replace(tzinfo=UTC)
    return None


def cost_so_far(created: str, hourly: float, monthly: float, now: datetime) -> float:
    """Estimate charges: Linode bills each started hour, capped at the monthly price."""
    start = datetime.fromisoformat(created).replace(tzinfo=UTC)
    hours = max(1, -(-(now - start) // timedelta(hours=1)))
    return min(hours * hourly, monthly)
