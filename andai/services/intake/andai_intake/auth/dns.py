"""DNS for sender verification: the intake's only egress (D-56).

SPF, DKIM, ARC and DMARC look up names that the sender chooses. Every lookup goes through one
`Resolver`:

* `DnsResolver` uses dnspython. It asks the servers in `ANDAI_INTAKE_DNS_RESOLVER` (comma-separated
  `host` or `host:port`, IPv6 as `[addr]:port`), or the container's own resolver when that is empty.
  Each lookup is capped at `ANDAI_INTAKE_DNS_TIMEOUT_S`. Answers are cached for their TTL (at most 5
  minutes, misses for 1 minute, at most 2048 entries).
* `StubResolver` serves a fixed zone and records what was asked. Tests use it, so they make no
  network calls.

A message is checked within one `Session`. At most `ANDAI_INTAKE_DNS_MAX_QUERIES` lookups reach the
resolver, repeats are answered from the session, and nothing is asked after the session's deadline.
A lookup that fails, times out, or exceeds the cap or deadline raises `DnsTempError`. The checks
report it as `temperror`, which never counts as a pass. An empty answer is a normal result.

`run_capped` runs a library check in a daemon thread and stops waiting at the session deadline.
`Session.dkim_txt` and `Session.spf_records` give dkimpy (`dnsfunc`) and pyspf (`DNSLookup`) their
answers in the shapes those libraries expect.
"""

from __future__ import annotations

import ipaddress
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from typing import Any, Protocol, TypeVar

import dns.exception
import dns.nameserver
import dns.resolver

RDTYPES = ("TXT", "A", "AAAA", "MX", "PTR")
CACHE_MAX_ENTRIES = 2048
CACHE_MAX_TTL_S = 300
NEGATIVE_TTL_S = 60
TXT_CHUNK = 255

T = TypeVar("T")


class DnsTempError(Exception):
    """A lookup that failed or timed out; never the same as an empty answer."""


class QueryCapExceeded(DnsTempError):
    """The message has used its lookups."""


class DeadlineExceeded(DnsTempError):
    """The message's checks ran out of time."""


class Resolver(Protocol):
    def lookup(self, name: str, rdtype: str) -> tuple[Any, ...]:
        """The records of `name` (lower-case, no trailing dot) of type `rdtype`, in this shape:
        TXT a tuple of each record's character-strings (bytes), A/AAAA the address text, MX
        `(preference, exchange)`, PTR the target name. Empty when the name or type has no record;
        raises `DnsTempError` when the answer could not be had."""


def normalise(name: str | bytes) -> str:
    if isinstance(name, bytes):
        name = name.decode("ascii", "replace")
    return name.strip().rstrip(".").lower()


def parse_nameservers(raw: str) -> tuple[tuple[str, int], ...]:
    """`ANDAI_INTAKE_DNS_RESOLVER`: comma-separated `host`, `host:port` or `[ipv6]:port`."""
    servers = []
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        host, port = item, 53
        if item.startswith("["):
            host, sep, rest = item[1:].partition("]")
            if not sep or (rest and not rest.startswith(":")):
                raise ValueError(f"bad DNS server {item!r}")
            if rest:
                port = int(rest[1:])
        elif item.count(":") == 1:
            host, _, port_text = item.partition(":")
            port = int(port_text)
        ipaddress.ip_address(host)  # a server is an address, never a name to resolve first
        if not 0 < port < 65536:
            raise ValueError(f"bad DNS port in {item!r}")
        servers.append((host, port))
    return tuple(servers)


def _records(rrset: Any, rdtype: str) -> tuple[Any, ...]:
    if rdtype == "TXT":
        return tuple(tuple(bytes(s) for s in rdata.strings) for rdata in rrset)
    if rdtype in ("A", "AAAA"):
        return tuple(rdata.address for rdata in rrset)
    if rdtype == "MX":
        return tuple(
            (int(rdata.preference), rdata.exchange.to_text(omit_final_dot=True).lower())
            for rdata in rrset
        )
    if rdtype == "PTR":
        return tuple(rdata.target.to_text(omit_final_dot=True).lower() for rdata in rrset)
    raise ValueError(f"unsupported record type {rdtype}")


class DnsResolver:
    """dnspython against the configured servers, with a per-lookup timeout and a TTL cache."""

    def __init__(
        self,
        nameservers: Sequence[tuple[str, int]] = (),
        *,
        timeout_s: float = 3.0,
        clock: Callable[[], float] = time.monotonic,
        cache_max: int = CACHE_MAX_ENTRIES,
    ) -> None:
        resolver = dns.resolver.Resolver(configure=not nameservers)
        if nameservers:
            resolver.nameservers = [
                dns.nameserver.Do53Nameserver(host, port) for host, port in nameservers
            ]
        resolver.timeout = timeout_s
        resolver.lifetime = timeout_s
        resolver.search = []
        resolver.cache = None
        self._resolver = resolver
        self.timeout_s = timeout_s
        self._clock = clock
        self._cache_max = cache_max
        self._cache: OrderedDict[tuple[str, str], tuple[float, tuple[Any, ...]]] = OrderedDict()
        self._lock = threading.Lock()

    def _cached(self, key: tuple[str, str]) -> tuple[Any, ...] | None:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if entry[0] <= self._clock():
                del self._cache[key]
                return None
            self._cache.move_to_end(key)
            return entry[1]

    def _store(self, key: tuple[str, str], records: tuple[Any, ...], ttl: float) -> None:
        with self._lock:
            self._cache[key] = (self._clock() + ttl, records)
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_max:
                self._cache.popitem(last=False)

    def lookup(self, name: str, rdtype: str) -> tuple[Any, ...]:
        if rdtype not in RDTYPES:
            raise ValueError(f"unsupported record type {rdtype}")
        key = (normalise(name), rdtype)
        cached = self._cached(key)
        if cached is not None:
            return cached
        try:
            answer = self._resolver.resolve(
                key[0] + ".", rdtype, search=False, raise_on_no_answer=False,
                lifetime=self.timeout_s,
            )
        except dns.resolver.NXDOMAIN:
            records: tuple[Any, ...] = ()
            ttl: float = NEGATIVE_TTL_S
        except dns.exception.DNSException as exc:  # timeout, SERVFAIL, no servers, bad answer
            raise DnsTempError(f"{rdtype} {key[0]}: {type(exc).__name__}") from None
        else:
            if answer.rrset is None:
                records, ttl = (), NEGATIVE_TTL_S
            else:
                records = _records(answer.rrset, rdtype)
                ttl = min(float(answer.rrset.ttl), CACHE_MAX_TTL_S)
        if ttl > 0:
            self._store(key, records, ttl)
        return records


def _txt_strings(value: str | bytes) -> tuple[bytes, ...]:
    data = value.encode("ascii") if isinstance(value, str) else value
    if not data:
        return (b"",)
    return tuple(data[i : i + TXT_CHUNK] for i in range(0, len(data), TXT_CHUNK))


class StubResolver:
    """A fixed zone, for tests: no network. `queries` records every lookup in order."""

    def __init__(self) -> None:
        self._zone: dict[tuple[str, str], list[Any]] = {}
        self._failing: set[str] = set()
        self._delay: dict[str, float] = {}
        self.queries: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def add(self, name: str, rdtype: str, *values: Any) -> StubResolver:
        """Add records; TXT values are text (split into 255-byte strings) or tuples of bytes."""
        if rdtype not in RDTYPES:
            raise ValueError(f"unsupported record type {rdtype}")
        records = self._zone.setdefault((normalise(name), rdtype), [])
        for value in values:
            if rdtype == "TXT" and not isinstance(value, tuple):
                value = _txt_strings(value)
            elif rdtype in ("MX",):
                value = (int(value[0]), normalise(value[1]))
            elif rdtype == "PTR":
                value = normalise(value)
            records.append(value)
        return self

    def txt(self, name: str, *values: str | bytes) -> StubResolver:
        return self.add(name, "TXT", *values)

    def remove(self, name: str, rdtype: str = "TXT") -> StubResolver:
        self._zone.pop((normalise(name), rdtype), None)
        return self

    def fail(self, name: str) -> StubResolver:
        """Every lookup of `name` fails as a timeout would."""
        self._failing.add(normalise(name))
        return self

    def slow(self, name: str, seconds: float) -> StubResolver:
        """Every lookup of `name` takes `seconds` before it answers."""
        self._delay[normalise(name)] = seconds
        return self

    def lookup(self, name: str, rdtype: str) -> tuple[Any, ...]:
        key = (normalise(name), rdtype)
        with self._lock:
            self.queries.append(key)
        delay = self._delay.get(key[0])
        if delay:
            time.sleep(delay)
        if key[0] in self._failing:
            raise DnsTempError(f"{rdtype} {key[0]}: stub failure")
        return tuple(self._zone.get(key, ()))


class Session:
    """One message's lookups: a query cap, a deadline, and repeats answered from the session."""

    def __init__(
        self,
        resolver: Resolver,
        *,
        max_queries: int = 30,
        deadline_s: float = 20.0,
        timeout_s: float = 3.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.resolver = resolver
        self.max_queries = max_queries
        self.timeout_s = timeout_s
        self._clock = clock
        self._deadline = clock() + deadline_s
        self._memo: dict[tuple[str, str], tuple[Any, ...] | DnsTempError] = {}
        self._lock = threading.Lock()
        self._cancelled = False
        self.queries = 0
        self.temperror = False
        self.cap_hit = False
        self.deadline_hit = False

    def remaining(self) -> float:
        return max(0.0, self._deadline - self._clock())

    def cancel(self) -> None:
        """Stop every later lookup (the deadline passed while a check was running)."""
        with self._lock:
            self._cancelled = True
            self.deadline_hit = True
            self.temperror = True

    def lookup(self, name: str | bytes, rdtype: str) -> tuple[Any, ...]:
        key = (normalise(name), rdtype)
        with self._lock:
            known = self._memo.get(key)
            if isinstance(known, DnsTempError):
                raise known
            if known is not None:
                return known
            if self._cancelled or self._clock() >= self._deadline:
                self.deadline_hit = True
                self.temperror = True
                raise DeadlineExceeded("deadline passed")
            if self.queries >= self.max_queries:
                self.cap_hit = True
                self.temperror = True
                raise QueryCapExceeded(f"more than {self.max_queries} lookups")
            self.queries += 1
        try:
            records = self.resolver.lookup(key[0], rdtype)
        except DnsTempError as exc:
            with self._lock:
                self.temperror = True
                self._memo[key] = exc
            raise
        with self._lock:
            self._memo[key] = records
        return records

    def txt(self, name: str | bytes) -> list[bytes]:
        """Each TXT record of `name`, its strings joined."""
        return [b"".join(strings) for strings in self.lookup(name, "TXT")]

    def dkim_txt(self, name: bytes | str) -> bytes | None:
        """dkimpy's `dnsfunc` answer: the first TXT record, or None."""
        records = self.txt(name)
        return records[0] if records else None

    def spf_records(self, name: str, qtype: str) -> list[tuple[tuple[str, str], Any]]:
        """pyspf's `DNSLookup` answer: `((name, qtype), value)` pairs; TXT values are the tuple of
        strings. pyspf asks for the SPF type only in its harsh mode, which is not used."""
        if qtype not in RDTYPES:
            return []
        return [((name, qtype), value) for value in self.lookup(name, qtype)]

    def summary(self) -> dict[str, Any]:
        return {
            "queries": self.queries,
            "temperror": self.temperror,
            "cap_hit": self.cap_hit,
            "deadline_hit": self.deadline_hit,
        }


def run_capped(fn: Callable[[], T], session: Session) -> T:
    """Run `fn` in a daemon thread; give up when the session's deadline passes."""
    if session.remaining() <= 0:
        session.cancel()
        raise DeadlineExceeded("no time left for the check")
    outcome: dict[str, Any] = {}

    def target() -> None:
        try:
            outcome["value"] = fn()
        except BaseException as exc:  # handed to the caller
            outcome["error"] = exc

    worker = threading.Thread(target=target, name="andai-intake-auth", daemon=True)
    worker.start()
    worker.join(session.remaining())
    if worker.is_alive():
        session.cancel()
        raise DeadlineExceeded("check did not finish before the deadline")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]
