"""Tests for best-effort VPN interface detection."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from net_connector.vpn import find_active_vpn_interfaces, looks_like_vpn


@pytest.mark.parametrize(
    "name",
    [
        "WireGuard Tunnel",
        "wg0",
        "tun0",
        "tun-portal",
        "TAP-Windows Adapter V9",
        "ppp0",
        "Cisco AnyConnect",
        "GlobalProtect",
        "Fortinet Virtual Adapter",
        "FortiClient Virtual Adapter",
        "Pulse Secure",
        "Pulse Virtual Adapter",
        "ProtonVPN",
        "Tailscale",
        "ZeroTier",
        "OpenVPN",
        "Corporate VPN Connection",
    ],
)
def test_looks_like_vpn_recognizes_active_user_tunnel_interfaces(name: str):
    assert looks_like_vpn(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "Ethernet",
        "Wi-Fi",
        "lo",
        "",
        "   ",
        "isatap",
        "Teredo",
        "6to4",
        pytest.param("notuninterface", id="contains-tun-not-prefix"),
        pytest.param("notapdevice", id="contains-tap-not-prefix"),
        pytest.param("nowgdevice", id="contains-wg-not-prefix"),
        pytest.param("noppplink", id="contains-ppp-not-prefix"),
        pytest.param("impulse audio", id="contains-pulse-not-token"),
        "isatap.0A1B2C3D VPN",
        "Teredo Tunneling Pseudo-Interface VPN",
        "6to4 Adapter VPN",
    ],
)
def test_looks_like_vpn_excludes_regular_and_system_tunnel_interfaces(name: str):
    assert looks_like_vpn(name) is False


def test_find_active_vpn_interfaces_filters_down_interfaces_and_sorts_case_insensitively():
    def stats_provider():
        return {
            "zetaVPN": SimpleNamespace(isup=True),
            "Tun0": SimpleNamespace(isup=True),
            "Ethernet": SimpleNamespace(isup=True),
            "OpenVPN down": SimpleNamespace(isup=False),
            "anyconnect": SimpleNamespace(isup=True),
        }

    assert find_active_vpn_interfaces(stats_provider) == ("anyconnect", "Tun0", "zetaVPN")


def test_find_active_vpn_interfaces_uses_a_tiebreaker_for_casefold_equal_names():
    first_order = {
        "tun0": SimpleNamespace(isup=True),
        "Tun0": SimpleNamespace(isup=True),
    }
    reverse_order = {
        "Tun0": SimpleNamespace(isup=True),
        "tun0": SimpleNamespace(isup=True),
    }

    assert find_active_vpn_interfaces(lambda: first_order) == ("Tun0", "tun0")
    assert find_active_vpn_interfaces(lambda: reverse_order) == ("Tun0", "tun0")


@pytest.mark.parametrize("error", [OSError("unavailable"), RuntimeError("unavailable")])
def test_find_active_vpn_interfaces_returns_empty_tuple_when_provider_fails(error: Exception):
    def stats_provider():
        raise error

    assert find_active_vpn_interfaces(stats_provider) == ()


def test_find_active_vpn_interfaces_requires_isup_to_be_true():
    def stats_provider():
        return {
            "tun0": SimpleNamespace(isup="false"),
            "tun1": SimpleNamespace(isup=1),
            "tun2": SimpleNamespace(isup=None),
            "tun3": SimpleNamespace(),
            "tun4": SimpleNamespace(isup=True),
        }

    assert find_active_vpn_interfaces(stats_provider) == ("tun4",)


def test_find_active_vpn_interfaces_skips_malformed_entries_without_discarding_valid_ones():
    class BrokenStat:
        @property
        def isup(self):
            raise RuntimeError("broken stat")

    class StringableName:
        def __str__(self):
            return "tun9"

    def stats_provider():
        return {
            "tun0": SimpleNamespace(isup=True),
            "tun1": BrokenStat(),
            StringableName(): SimpleNamespace(isup=True),
        }

    assert find_active_vpn_interfaces(stats_provider) == ("tun0",)


def test_find_active_vpn_interfaces_rejects_non_mapping_provider_data():
    assert find_active_vpn_interfaces(lambda: [("tun0", SimpleNamespace(isup=True))]) == ()


def test_find_active_vpn_interfaces_returns_empty_tuple_when_mapping_enumeration_fails():
    from collections.abc import Mapping

    class BrokenMapping(Mapping):
        def __getitem__(self, key):
            raise KeyError(key)

        def __iter__(self):
            return iter(())

        def __len__(self):
            return 0

        def items(self):
            raise RuntimeError("items unavailable")

    assert find_active_vpn_interfaces(BrokenMapping) == ()
