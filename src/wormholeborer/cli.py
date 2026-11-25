"""Command-line interface: wormholeborer up | down | status."""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from wormholeborer import ops
from wormholeborer.cloudinit import MAILBOX_PORT, TRANSIT_PORT
from wormholeborer.linode import LinodeClient, LinodeError

BOOT_TIMEOUT = 300
SETUP_TIMEOUT = 900


def _client(args: argparse.Namespace) -> LinodeClient:
    return LinodeClient(ops.read_secret_file(args.token_file, "API token"))


def cmd_up(args: argparse.Namespace) -> None:
    """Create a server, wait until it serves, and print client flags."""
    existing = ops.Server.load(args.state_file)
    if existing:
        raise ops.OpsError(
            f"server {existing.label} ({existing.ipv4}) is already recorded in {args.state_file};"
            " run `wormholeborer down` first"
        )
    ttl = ops.parse_ttl(args.ttl)
    pubkey = ops.read_secret_file(args.ssh_key, "SSH public key")
    client = _client(args)
    ops.check_region(client, args.region)
    server = ops.create_server(
        client, region=args.region, linode_type=args.type, ttl=ttl, pubkey=pubkey
    )
    server.save(args.state_file)
    print(f"created linode {server.linode_id} at {server.ipv4}; waiting for boot", flush=True)
    ops.wait_until_running(client, server.linode_id, BOOT_TIMEOUT)
    print("booted; waiting for cloud-init to start the relay services", flush=True)
    ops.wait_for_ports(server.ipv4, [MAILBOX_PORT, TRANSIT_PORT], SETUP_TIMEOUT)
    flags = (
        f"--relay-url ws://{server.ipv4}:{MAILBOX_PORT}/v1"
        f" --transit-helper tcp:{server.ipv4}:{TRANSIT_PORT}"
    )
    print(
        f"\nready. Self-destructs at {server.expires_at} unless you run `wormholeborer down`.\n\n"
        f"  wormhole {flags} send FILE\n"
        f"  wormhole {flags} receive\n\n"
        f"  ssh root@{server.ipv4}"
    )


def cmd_down(args: argparse.Namespace) -> None:
    """Delete the recorded server, or every tagged server with --all."""
    client = _client(args)
    if args.all:
        lines = ops.destroy_all(client)
    else:
        server = ops.Server.load(args.state_file)
        if server is None:
            raise ops.OpsError(f"no server recorded in {args.state_file}; use --all to sweep")
        lines = ops.destroy(client, server)
    args.state_file.unlink(missing_ok=True)
    print("\n".join(lines))


def cmd_status(args: argparse.Namespace) -> None:
    """List tagged servers with their expiry and cost so far."""
    client = _client(args)
    servers = ops.list_servers(client)
    if not servers:
        print("no wormholeborer servers running")
        return
    now = datetime.now(UTC)
    for inst in servers:
        price = client.request("GET", f"/linode/types/{inst['type']}")["price"]
        expiry = ops.expiry_from_tags(inst["tags"])
        cost = ops.cost_so_far(inst["created"], price["hourly"], price["monthly"], now)
        expires = f"expires {expiry:%Y-%m-%d %H:%M} UTC" if expiry else "no expiry tag"
        print(
            f"{inst['id']}  {inst['label']}  {inst['region']}  {inst['ipv4'][0]}"
            f"  {inst['status']}  {expires}  ~${cost:.2f} so far"
        )


def build_parser() -> argparse.ArgumentParser:
    """Define the command-line arguments."""
    parser = argparse.ArgumentParser(prog="wormholeborer", description=__doc__)
    parser.add_argument("--token-file", type=Path, default=Path("akamai.key"))
    parser.add_argument("--state-file", type=Path, default=Path(".wormholeborer-state.json"))
    sub = parser.add_subparsers(required=True)

    up = sub.add_parser("up", help="create a relay server")
    up.add_argument("--region", default="us-east")
    up.add_argument("--type", default="g6-nanode-1", help="Linode plan")
    up.add_argument("--ttl", default="12h", help="self-destruct after this long (10m to 7d)")
    up.add_argument("--ssh-key", type=Path, default=Path("id_ed25519.pub"))
    up.set_defaults(func=cmd_up)

    down = sub.add_parser("down", help="delete the relay server")
    down.add_argument("--all", action="store_true", help="delete every wormholeborer server")
    down.set_defaults(func=cmd_down)

    status = sub.add_parser("status", help="list relay servers")
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return an exit code."""
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (ops.OpsError, LinodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "\ninterrupted; the server (if created) is still running. Run `wormholeborer down`.",
            file=sys.stderr,
        )
        return 130
    return 0
