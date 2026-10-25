"""The client address of a request.

`X-Forwarded-For` is believed only when the TCP peer is a trusted proxy (`ANDAI_TRUSTED_PROXIES`:
comma-separated CIDRs, addresses or hostnames; in the stack, the portal). From a trusted peer the
header is read right to left, skipping trusted hops, and the first other address is the client.
From anyone else the header is ignored and the peer address is the client. Hostnames are resolved
through DNS and the answer (or the failure) is cached for 30 seconds.

The result feeds the login limiter key, `sessions.ip` and audit events.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from collections.abc import Callable, Iterable

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

RESOLVE_TTL_SECONDS = 30.0


def _system_resolve(host: str) -> set[str]:
    return {info[4][0] for info in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)}


def _parse_ip(value: str) -> IPAddress | None:
    value = value.strip()
    if value.startswith("[") and "]" in value:  # [v6]:port
        value = value[1 : value.index("]")]
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


class ClientIpResolver:
    def __init__(
        self,
        trusted: Iterable[str],
        *,
        resolve: Callable[[str], set[str]] = _system_resolve,
        clock: Callable[[], float] = time.monotonic,
        ttl: float = RESOLVE_TTL_SECONDS,
    ) -> None:
        self._networks: list[IPNetwork] = []
        self._hosts: list[str] = []
        for entry in (e.strip() for e in trusted):
            if not entry:
                continue
            try:
                self._networks.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                self._hosts.append(entry.lower())
        self._resolve = resolve
        self._clock = clock
        self._ttl = ttl
        self._cache: dict[str, tuple[float, frozenset[IPAddress]]] = {}
        self._lock = threading.Lock()

    def _host_addresses(self, host: str) -> frozenset[IPAddress]:
        now = self._clock()
        with self._lock:
            cached = self._cache.get(host)
            if cached is not None and now - cached[0] < self._ttl:
                return cached[1]
        try:
            found = frozenset(a for a in map(_parse_ip, self._resolve(host)) if a is not None)
        except OSError:
            found = frozenset()
        with self._lock:
            self._cache[host] = (now, found)
        return found

    def is_trusted(self, address: IPAddress) -> bool:
        if any(address in network for network in self._networks):
            return True
        return any(address in self._host_addresses(host) for host in self._hosts)

    def client_ip(self, peer: str | None, forwarded_for: str | None) -> str | None:
        """The client address, or None when neither the peer nor the header gives one."""
        peer_ip = _parse_ip(peer) if peer else None
        if peer_ip is None or not forwarded_for or not self.is_trusted(peer_ip):
            return None if peer_ip is None else str(peer_ip)
        hops = [_parse_ip(part) for part in forwarded_for.split(",")]
        if any(hop is None for hop in hops):
            return str(peer_ip)  # a malformed header is not believed at all
        for hop in reversed(hops):
            if not self.is_trusted(hop):
                return str(hop)
        return str(hops[0])
