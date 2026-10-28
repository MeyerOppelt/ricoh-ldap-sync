"""Read the embedded tailscaled's state through the tailscale CLI."""

from __future__ import annotations

import ipaddress
import json
import subprocess
from dataclasses import dataclass


class TailnetError(Exception):
    pass


@dataclass(frozen=True)
class Route:
    peer: str
    prefix: str
    online: bool


def _cli(*args: str) -> dict:
    try:
        out = subprocess.run(["tailscale", *args], capture_output=True, text=True, timeout=10, check=True).stdout
        return json.loads(out)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as e:
        raise TailnetError(f"tailscale {' '.join(args)} failed: {e}") from e


def status() -> dict:
    return _cli("status", "--json")


def prefs() -> dict:
    return _cli("debug", "prefs")


def host_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The IP in a printer host string, or None if it is a hostname."""
    candidate = host
    if candidate.startswith("["):  # [v6]:port
        candidate = candidate[1 : candidate.find("]")]
    elif candidate.count(":") == 1:  # v4:port or name:port
        candidate = candidate.split(":")[0]
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def route_for(ip, st: dict) -> Route | None:
    """The peer whose primary (approved, active) subnet route covers ip, if any."""
    best: Route | None = None
    best_len = -1
    for peer in (st.get("Peer") or {}).values():
        for prefix in peer.get("PrimaryRoutes") or []:
            net = ipaddress.ip_network(prefix, strict=False)
            if ip.version == net.version and ip in net and net.prefixlen > best_len:
                name = peer.get("HostName") or peer.get("DNSName") or "?"
                best, best_len = Route(peer=name, prefix=prefix, online=bool(peer.get("Online"))), net.prefixlen
    return best
