"""Accesso limitato a Tailscale: scelta dell'indirizzo su cui ascoltare e filtro dei client."""
import ipaddress
import time
from typing import Callable

import psutil

# Reti usate da Tailscale (CGNAT IPv4 e ULA IPv6) più il loopback (necessario per `tailscale serve`).
DEFAULT_ALLOWED = "100.64.0.0/10,fd7a:115c:a1e0::/48,127.0.0.0/8,::1/128"
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def parse_networks(text: str) -> list | None:
    """Elenco di reti ammesse; None = nessun filtro (valore 'any')."""
    text = (text or "").strip()
    if text.lower() in ("any", "all", "*"):
        return None
    nets = [ipaddress.ip_network(p.strip(), strict=False) for p in text.split(",") if p.strip()]
    if not nets:
        raise ValueError("ALLOWED_NETWORKS è vuota")
    return nets


def is_allowed(client: str | None, nets: list | None) -> bool:
    if nets is None:
        return True
    try:
        ip = ipaddress.ip_address((client or "").split("%")[0])
    except ValueError:
        return False                       # host non numerico: nega
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped                # ::ffff:100.x.y.z
    return any(ip in n for n in nets if n.version == ip.version)


def find_tailscale_ip(iface: str = "tailscale0", addrs: dict | None = None) -> str | None:
    """Indirizzo IPv4 100.x.y.z dell'interfaccia Tailscale, se presente."""
    addrs = psutil.net_if_addrs() if addrs is None else addrs
    for a in addrs.get(iface, []):
        if getattr(a.family, "name", "") == "AF_INET":
            try:
                if ipaddress.ip_address(a.address) in _CGNAT:
                    return a.address
            except ValueError:
                continue
    return None


def resolve_bind(setting: str, iface: str = "tailscale0", wait: float = 120, interval: float = 3,
                 finder: Callable[[str], str | None] = find_tailscale_ip,
                 sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] = print) -> str:
    """'tailscale' = solo IP Tailscale (attende che l'interfaccia compaia, mai ripiego su 0.0.0.0);
    'all' = 0.0.0.0; altrimenti un indirizzo IP esplicito (es. 127.0.0.1)."""
    s = (setting or "tailscale").strip().lower()
    if s in ("all", "0.0.0.0"):
        return "0.0.0.0"
    if s != "tailscale":
        ipaddress.ip_address(s)            # solleva ValueError se non è un IP
        return s
    deadline = time.monotonic() + wait
    announced = False
    while True:
        ip = finder(iface)
        if ip:
            return ip
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Interfaccia Tailscale '{iface}' non trovata: Tailscale è attivo sul Raspberry?")
        if not announced:
            log(f"[pi-dashboard] In attesa dell'interfaccia Tailscale '{iface}'…")
            announced = True
        sleep(interval)
