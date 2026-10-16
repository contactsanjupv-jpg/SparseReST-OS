"""
Outbound-fetch safety. Prospect websites come from third-party data (OpenStreetMap tags, scraped pages,
CSV imports) -- i.e. untrusted. A hostile or mistaken URL must never make this server fetch something on
its own network (cloud metadata at 169.254.169.254, localhost admin ports, a private LAN).

Policy: only http(s); never an IP literal / hostname that is loopback, private, link-local, multicast or
reserved; and if DNS resolves to such an address, refuse. A hostname that does not resolve at all is
allowed through here (the fetch itself will simply fail) so offline development and tests keep working.
"""
from __future__ import annotations

import ipaddress
import socket
from typing import Callable, Optional
from urllib.parse import urlparse

BLOCKED_SUFFIXES = (".local", ".internal", ".localhost", ".lan", ".intranet", ".corp", ".home")


def _is_blocked_ip(raw: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified


def is_public_http_url(url: str, resolver: Optional[Callable[[str], list[str]]] = None) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith(BLOCKED_SUFFIXES):
        return False
    if _is_blocked_ip(host):
        return False
    try:
        ipaddress.ip_address(host)
        return True                       # a public IP literal
    except ValueError:
        pass
    resolve = resolver or _default_resolver
    try:
        addresses = resolve(host)
    except OSError:
        return True                       # does not resolve: the fetch will fail on its own
    return not any(_is_blocked_ip(a) for a in addresses)


def _default_resolver(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None)]
