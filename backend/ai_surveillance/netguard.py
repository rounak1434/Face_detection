"""Server-side request forgery guard for camera/NVR connection tests.

A device address must be a syntactically valid IP or host name, and every
address it resolves to must lie inside the allowed (private LAN) networks.
Connection tests then connect to the checked IP literal, never to the name,
so a DNS answer cannot change between the check and the connection.
"""

import ipaddress
import re
import socket
from collections.abc import Callable

_HOSTNAME = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class AddressNotAllowed(ValueError):
    pass


def parse_networks(spec: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    return [ipaddress.ip_network(part.strip(), strict=False) for part in spec.split(",") if part.strip()]


def validate_host_syntax(host: str) -> str:
    """Return the normalized host, or raise ValueError. Does not resolve."""
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    if not _HOSTNAME.match(host):
        raise ValueError("Enter an IP address or a host name (letters, digits, dots, hyphens)")
    return host.lower()


def _always_refused(ip: IPAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved


def check_ip(ip: IPAddress, allowed: list) -> None:
    if _always_refused(ip):
        raise AddressNotAllowed(f"{ip} is a loopback, link-local, multicast or reserved address")
    if not any(ip.version == net.version and ip in net for net in allowed):
        raise AddressNotAllowed(f"{ip} is outside the allowed device networks")


def _system_resolve(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


def resolve_allowed(host: str, allowed: list, resolver: Callable[[str], list[str]] | None = None) -> IPAddress:
    """Resolve `host`, require every address to be allowed, return the first."""
    resolver = resolver or _system_resolve
    host = validate_host_syntax(host)
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = [ipaddress.ip_address(a.split("%")[0]) for a in resolver(host)]
        except OSError as exc:
            raise AddressNotAllowed(f"Host name {host!r} could not be resolved") from exc
        if not addresses:
            raise AddressNotAllowed(f"Host name {host!r} has no addresses") from None
    for ip in addresses:
        check_ip(ip, allowed)
    return addresses[0]
