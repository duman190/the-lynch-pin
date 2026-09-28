"""Network guard: keep the portal on the local network.

Two checks, applied to every request:
  * the peer address must be loopback / private (RFC 1918, RFC 4193) / link-local;
  * the Host header must name this machine the way a LAN client would (localhost, a private
    IP literal, a single-label hostname or a *.local / *.lan / *.home.arpa name). This blunts
    DNS-rebinding, where a public web page resolves its own domain to a LAN IP.

Tailscale is allowed by default: its IPv4 range 100.64.0.0/10 (shared with ISP carrier-grade NAT,
whose other customers cannot open connections to your machine) and its IPv6 ULA range (in fc00::/7).
The rest of 100.0.0.0/8 is public internet space and stays refused. Other overlay networks can be added
with ``--allow-net CIDR`` / LYNCH_UI_ALLOWED_NETS, and full MagicDNS names (``*.ts.net``) are accepted
as Host headers.

This is a network boundary, not authentication: anyone on the same Wi-Fi can use the portal.
"""
import ipaddress
import os

_LAN_SUFFIXES = (".local", ".lan", ".home.arpa", ".localdomain", ".internal", ".ts.net")  # .ts.net = MagicDNS

LOCAL_NETS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "100.64.0.0/10",  # Tailscale (CGNAT range)
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


_CLI_NETS = []   # added at startup from --allow-net
_CLI_HOSTS = set()  # added at startup from --allow-host


def parse_nets(values):
    """``["100.0.0.0/8, 10.0.0.0/8", ...]`` → (networks, invalid entries)."""
    nets, bad = [], []
    for value in values or []:
        for raw in str(value).split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                nets.append(ipaddress.ip_network(raw, strict=False))
            except ValueError:
                bad.append(raw)
    return nets, bad


def allow(nets=(), hosts=()):
    """Extend the allow-lists for this process (CLI flags). Returns invalid network entries."""
    parsed, bad = parse_nets(nets)
    _CLI_NETS.extend(n for n in parsed if n not in _CLI_NETS)
    for h in hosts or []:
        _CLI_HOSTS.update(x.strip().lower() for x in str(h).split(",") if x.strip())
    return bad


def _extra_nets():
    return [*parse_nets([os.environ.get("LYNCH_UI_ALLOWED_NETS", "")])[0], *_CLI_NETS]


def extra_allowed():
    """Networks / host names allowed on top of the private ranges (for the startup banner)."""
    return [str(n) for n in _extra_nets()], sorted(_extra_hosts())


def is_local_client(addr):
    """True when the peer IP is loopback, RFC 1918 / RFC 4193 private, link-local or Tailscale."""
    ip = _ip(addr)
    if ip is None:
        return False
    return any(ip.version == n.version and ip in n for n in (*LOCAL_NETS, *_extra_nets()))


def _extra_hosts():
    raw = os.environ.get("LYNCH_UI_ALLOWED_HOSTS", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()} | _CLI_HOSTS


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
