"""Render the cloud-init user data that turns a fresh Debian 13 Linode into a wormhole server."""

import json
from datetime import UTC, datetime
from importlib.resources import files

MAILBOX_PORT = 4000
TRANSIT_PORT = 4001

SERVICE_USER = "wormhole"
STATE_DIR = "/var/lib/wormhole"
VENV = "/opt/wormhole"
CONF_DIR = "/etc/wormholeborer"

# Listen on "::" (IPv4 and IPv6). Twisted endpoints need each colon escaped as "\:",
# and systemd unit files need each backslash doubled.
LISTEN_ALL = "interface=\\\\:\\\\:"

SELF_DESTRUCT_SCRIPT = f"""#!/bin/sh
set -eu
md=http://169.254.169.254/v1
md_token=$(curl -fsS -X PUT -H 'Metadata-Token-Expiry-Seconds: 300' "$md/token")
id=$(curl -fsS -H "Metadata-Token: $md_token" -H 'Accept: application/json' "$md/instance" \\
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl -fsS -X DELETE -H @{CONF_DIR}/auth-header "https://api.linode.com/v4/linode/instances/$id"
"""

SSHD_CONFIG = """PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
"""


def _service_unit(description: str, command: str) -> str:
    return f"""[Unit]
Description={description}
After=network-online.target
Wants=network-online.target

[Service]
User={SERVICE_USER}
WorkingDirectory={STATE_DIR}
ExecStart={VENV}/bin/twist {command}
Restart=on-failure
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths={STATE_DIR}

[Install]
WantedBy=multi-user.target
"""


def _self_destruct_units(expires_at: datetime) -> tuple[str, str]:
    service = f"""[Unit]
Description=Delete this Linode through the API

[Service]
Type=oneshot
ExecStart={CONF_DIR}/self-destruct
Restart=on-failure
RestartSec=60
"""
    timer = f"""[Unit]
Description=Delete this Linode when its time-to-live expires

[Timer]
OnCalendar={expires_at.astimezone(UTC):%Y-%m-%d %H:%M:%S} UTC
Persistent=true
AccuracySec=1min

[Install]
WantedBy=timers.target
"""
    return service, timer


def _file(path: str, content: str, permissions: str = "0644") -> dict[str, str]:
    return {"path": path, "content": content, "permissions": permissions, "owner": "root:root"}


def render(api_token: str, expires_at: datetime) -> str:
    """Return cloud-config user data.

    Args:
        api_token: Linode token the server uses to delete itself.
        expires_at: When the server deletes itself.
    """
    requirements = files("wormholeborer").joinpath("server-requirements.txt").read_text()
    destruct_service, destruct_timer = _self_destruct_units(expires_at)
    units = "/etc/systemd/system"
    config = {
        "package_update": True,
        "packages": ["python3-venv", "curl"],
        "write_files": [
            _file("/etc/ssh/sshd_config.d/10-wormholeborer.conf", SSHD_CONFIG),
            _file(f"{CONF_DIR}/auth-header", f"Authorization: Bearer {api_token}\n", "0600"),
            _file(f"{CONF_DIR}/self-destruct", SELF_DESTRUCT_SCRIPT, "0700"),
            _file(f"{CONF_DIR}/requirements.txt", requirements),
            _file(f"{units}/wormholeborer-self-destruct.service", destruct_service),
            _file(f"{units}/wormholeborer-self-destruct.timer", destruct_timer),
            _file(
                f"{units}/wormhole-mailbox.service",
                _service_unit(
                    "Magic Wormhole mailbox server",
                    f"wormhole-mailbox --port=tcp:{MAILBOX_PORT}:{LISTEN_ALL}"
                    f" --channel-db={STATE_DIR}/relay.sqlite",
                ),
            ),
            _file(
                f"{units}/wormhole-transit.service",
                _service_unit(
                    "Magic Wormhole transit relay",
                    f"transitrelay --port=tcp:{TRANSIT_PORT}:{LISTEN_ALL}",
                ),
            ),
        ],
        # The self-destruct timer is armed first so the server still expires if setup fails.
        "runcmd": [
            "systemctl daemon-reload",
            "systemctl enable --now wormholeborer-self-destruct.timer",
            "systemctl reload ssh",
            f"useradd --system --create-home --home-dir {STATE_DIR}"
            f" --shell /usr/sbin/nologin {SERVICE_USER}",
            f"python3 -m venv {VENV}",
            f"{VENV}/bin/pip install --no-cache-dir --only-binary=:all: --require-hashes"
            f" -r {CONF_DIR}/requirements.txt",
            "systemctl enable --now wormhole-mailbox.service wormhole-transit.service",
        ],
    }
    return "#cloud-config\n" + json.dumps(config, indent=1)
