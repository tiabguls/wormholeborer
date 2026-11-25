# wormholeborer

Creates a disposable, private [Magic Wormhole](https://magic-wormhole.readthedocs.io/) server on Linode (Akamai Cloud), prints the client flags to use it, and deletes it when you are done. The server runs both the mailbox server (port 4000) and the transit relay (port 4001). It deletes itself after a time-to-live, so a forgotten server stops billing.

It uses a Nanode 1 GB by default: $0.0075/hour (capped at $5/month), with 1 TB/month of outbound transfer included. That covers about 100 GB/week of relayed transfers. Inbound traffic is free.

## Setup

- `akamai.key`: a Linode Personal Access Token with **Linodes: read/write** and **Account: read/write**. Account read/write is needed to create the server's own short-lived token.
- `id_ed25519.pub`: the SSH public key installed for `root`. Password login is disabled.

Both paths can be changed with `--token-file` and `up --ssh-key`.

## Usage

```sh
uv run wormholeborer up                  # us-east, 12h TTL; takes ~2-4 minutes
uv run wormholeborer up --region us-ord --ttl 4h
uv run wormholeborer status              # running servers, expiry, cost so far
uv run wormholeborer down                # delete the server and revoke its token
uv run wormholeborer down --all          # delete everything this tool created
```

`up` prints the flags for both ends of a transfer:

```sh
wormhole --relay-url ws://IP:4000/v1 --transit-helper tcp:IP:4001 send FILE
wormhole --relay-url ws://IP:4000/v1 --transit-helper tcp:IP:4001 receive
```

The receiver must use the same `--relay-url`, or the code will not be found.

## How it works

`up` creates a token scoped to `linodes:read_write` that expires one hour after the TTL. It then creates a Debian 13 Linode whose cloud-init user data:

1. arms a systemd timer that deletes the instance through the API at the TTL;
2. disables SSH password login;
3. installs `magic-wormhole-mailbox-server` and `magic-wormhole-transit-relay` into a venv from hash-pinned wheels (`src/wormholeborer/server-requirements.txt`), then runs them as an unprivileged `wormhole` user.

The server's IDs are saved in `.wormholeborer-state.json` so that `down` knows what to delete. Every server is also tagged `wormholeborer`, so `status` and `down --all` work without the state file.

## Security notes

- **The self-destruct token can delete any Linode on the account.** Linode has no per-instance token scopes. The token sits root-only on the server, expires an hour after the TTL, and is revoked by `down`. If the account runs other important Linodes, use a separate Linode account for this.
- The mailbox uses plain `ws://`, like the public server. File contents stay end-to-end encrypted by Wormhole's PAKE, and the relay never sees them.
- The relay ports are open to anyone who has the IP address.

## Updating the server packages

Edit `src/wormholeborer/server-requirements.in`, then run `scripts/lock-server-deps.sh`. It pins the exact wheels Debian 13 (CPython 3.13, x86_64) installs, with hashes. A full multi-platform lock would not fit in Linode's 64 KB user-data limit.

## Development

```sh
uv run pytest -q && uv run ruff check && uv run ruff format --check && uv run ty check
```
