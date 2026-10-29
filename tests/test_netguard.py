import socket
from types import SimpleNamespace as NS

import pytest

from app import netguard


def test_allowed_networks_default_is_tailscale_lan_and_loopback():
    nets = netguard.parse_networks(netguard.DEFAULT_ALLOWED)
    ok = ["100.64.0.0", "100.100.100.100", "100.127.255.255", "127.0.0.1", "::1", "fd7a:115c:a1e0:ab12::1", "::ffff:100.80.1.2",
          "192.168.1.10", "192.168.255.255", "10.1.2.3", "172.16.0.1", "172.31.255.254", "fe80::1", "fd12:3456::1", "::ffff:192.168.0.5"]
    no = ["100.63.255.255", "100.128.0.0", "172.15.255.255", "172.32.0.1", "192.169.0.1", "11.0.0.1", "8.8.8.8", "1.1.1.1",
          "2001:db8::1", "2a00:1450::1", "::ffff:8.8.8.8", "", None, "non-un-ip"]
    assert all(netguard.is_allowed(i, nets) for i in ok)
    assert not any(netguard.is_allowed(i, nets) for i in no)


def test_named_groups_can_be_mixed_with_cidr():
    tail_only = netguard.parse_networks("tailscale,loopback")           # il vecchio comportamento: niente LAN
    assert netguard.is_allowed("100.90.1.1", tail_only) and netguard.is_allowed("127.0.0.1", tail_only)
    assert not netguard.is_allowed("192.168.1.10", tail_only)
    mixed = netguard.parse_networks("Loopback, 192.168.1.0/24, tailscale")
    assert netguard.is_allowed("192.168.1.77", mixed) and not netguard.is_allowed("192.168.2.1", mixed)
    assert not netguard.is_allowed("10.0.0.1", mixed)


def test_any_disables_filter_and_empty_is_error():
    assert netguard.is_allowed("192.168.1.1", netguard.parse_networks("any"))
    with pytest.raises(ValueError):
        netguard.parse_networks(" , ")


def _addr(fam, ip):
    return NS(family=fam, address=ip)


def test_find_tailscale_ip():
    addrs = {"tailscale0": [_addr(socket.AF_INET6, "fd7a:115c:a1e0::1"), _addr(socket.AF_INET, "100.88.12.4")],
             "eth0": [_addr(socket.AF_INET, "192.168.1.20")]}
    assert netguard.find_tailscale_ip("tailscale0", addrs) == "100.88.12.4"
    assert netguard.find_tailscale_ip("wlan0", addrs) is None
    # un'interfaccia con nome giusto ma IP non Tailscale non viene accettata
    assert netguard.find_tailscale_ip("eth0", {"eth0": [_addr(socket.AF_INET, "192.168.1.20")]}) is None


def test_resolve_bind_modes():
    assert netguard.resolve_bind("all") == "0.0.0.0"
    assert netguard.resolve_bind("127.0.0.1") == "127.0.0.1"
    with pytest.raises(ValueError):
        netguard.resolve_bind("non-un-ip")
    assert netguard.resolve_bind("tailscale", finder=lambda i: "100.1.2.3") == "100.1.2.3"


def test_resolve_bind_waits_for_interface_then_fails_closed():
    calls = []
    answers = iter([None, None, "100.9.9.9"])
    ip = netguard.resolve_bind("tailscale", wait=60, interval=0, finder=lambda i: next(answers),
                               sleep=lambda s: calls.append(s), log=lambda m: calls.append(m))
    assert ip == "100.9.9.9" and len(calls) == 3        # un solo messaggio + due attese
    # mai Tailscale: errore, non ripiego su 0.0.0.0
    with pytest.raises(RuntimeError):
        netguard.resolve_bind("tailscale", wait=0, interval=0, finder=lambda i: None, log=lambda m: None)
