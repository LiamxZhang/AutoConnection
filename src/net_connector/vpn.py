"""Best-effort detection of active user VPN interfaces."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Callable

import psutil


_SYSTEM_TUNNEL_PATTERN = re.compile(
    r"^(?:isatap(?:\.|$)|teredo(?:$|\s+tunneling\s+pseudo-interface(?:$|\s))|6to4(?:$|[\s._-]))"
)
_SHORT_INTERFACE_PATTERN = re.compile(r"^(?:tun|tap|ppp|wg)(?:$|\d|[-_ ])")
_PULSE_TOKEN_PATTERN = re.compile(r"(?<![a-z0-9])pulse(?![a-z0-9])")
_VPN_MARKERS = (
    "wireguard",
    "openvpn",
    "anyconnect",
    "globalprotect",
    "fortinet",
    "forticlient",
    "pulse secure",
    "protonvpn",
    "tailscale",
    "zerotier",
    "vpn",
)


def looks_like_vpn(name: str) -> bool:
    """Return whether an interface name indicates a user VPN or tunnel."""
    normalized = str(name).strip().casefold()
    if not normalized or _SYSTEM_TUNNEL_PATTERN.match(normalized):
        return False
    return bool(_SHORT_INTERFACE_PATTERN.match(normalized)) or bool(_PULSE_TOKEN_PATTERN.search(normalized)) or any(
        marker in normalized for marker in _VPN_MARKERS
    )


def find_active_vpn_interfaces(
    stats_provider: Callable[[], object] | None = None,
) -> tuple[str, ...]:
    """Return active VPN interface names, without allowing diagnosis to fail callers."""
    provider = stats_provider if stats_provider is not None else psutil.net_if_stats
    try:
        stats = provider()
        if not isinstance(stats, Mapping):
            return ()
        entries = tuple(stats.items())
    except Exception:
        return ()

    active_vpn_names = []
    for entry in entries:
        try:
            name, stat = entry
            if not isinstance(name, str) or getattr(stat, "isup", None) is not True:
                continue
            if looks_like_vpn(name):
                active_vpn_names.append(name)
        except Exception:
            continue
    return tuple(sorted(active_vpn_names, key=lambda name: (name.casefold(), name)))
