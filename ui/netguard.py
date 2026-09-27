"""Network guard: keep the portal on the local network.

Two checks, applied to every request:
  * the peer address must be loopback / private (RFC 1918, RFC 4193) / link-local;
  * the Host header must name this machine the way a LAN client would (localhost, a private
    IP literal, a single-label hostname or a *.local / *.lan / *.home.arpa name). This blunts
    DNS-rebinding, where a public web page resolves its own domain to a LAN IP.

Tailscale / CGNAT (100.64.0.0/10) or other overlay networks are refused by default; add them
with LYNCH_UI_ALLOWED_NETS="100.64.0.0/10" (and their MagicDNS names with LYNCH_UI_ALLOWED_HOSTS).

This is a network boundary, not authentication: anyone on the same Wi-Fi can use the portal.
"""
import ipaddress
import os

_LAN_SUFFIXES = (".local", ".lan", ".home.arpa", ".localdomain", ".internal")

LOCAL_NETS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "::1/128", "fc00::/7", "fe80::/10",
))


def _ip(value):
    try:
        ip = ipaddress.ip_address(str(value).split("%", 1)[0])
    except (ValueError, AttributeError):
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip


def _extra_nets():
    nets = []
    for raw in os.environ.get("LYNCH_UI_ALLOWED_NETS", "").split(","):
        raw = raw.strip()
        if raw:
            try:
                nets.append(ipaddress.ip_network(raw, strict=False))
            except ValueError:
                pass
    return nets


def is_local_client(addr):
    """True when the peer IP is loopback, RFC 1918 / RFC 4193 private or link-local."""
    ip = _ip(addr)
    if ip is None:
        return False
    return any(ip.version == n.version and ip in n for n in (*LOCAL_NETS, *_extra_nets()))


def _extra_hosts():
    raw = os.environ.get("LYNCH_UI_ALLOWED_HOSTS", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def is_allowed_host(host_header):
    """True when the Host header is something a LAN client would legitimately send."""
    if not host_header:
        return False
    host = host_header.strip().lower()
    if host.startswith("["):  # [v6]:port
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    host = host.rstrip(".")
    if not host:
        return False
    if host in _extra_hosts() or host == "localhost":
        return True
    ip = _ip(host)
    if ip is not None:
        return is_local_client(str(ip))
    if "." not in host:  # single-label machine name, e.g. "my-desktop"
        return all(c.isalnum() or c == "-" for c in host)
    return host.endswith(_LAN_SUFFIXES)
