"""A stub DNS server for the e2e stack: the `fake-dns` service of the `e2e` compose profile only.

    python -m andai_intake.testing.fake_dns --zone /tmp/fake-dns/zone.json \\
        --host 127.0.0.1 --port 5353

It answers TXT and A questions from a zone file (`sign.FileZone`, written by
`python -m andai_intake.testing.sign`) over UDP and TCP, and re-reads the file on every query, so a
record the signer has just written is answered on the next lookup. A name with records of another
type gets an empty answer; a name not in the file gets NXDOMAIN. Answers carry a TTL of 1 s, so
the intake's cache never holds a replaced key for long. A UDP answer larger than the asker allows
(512 bytes, or its EDNS size) is sent truncated, and the asker repeats the question over TCP, as
with any DNS server.

In the e2e stack it shares andai-intake's network namespace (`network_mode: service:andai-intake`)
and listens on 127.0.0.1, so it is reachable only from there, and the e2e env file sets
`ANDAI_INTAKE_DNS_RESOLVER=127.0.0.1:5353` (the resolver takes addresses, never names). It reads
nothing but the zone file and is never part of the default stack.

`--probe` asks the running server one question and exits 0 when it answers (the compose health
check). Message parsing is dnspython's; nothing here is new protocol code.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import struct
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import dns.exception
import dns.flags
import dns.message
import dns.name
import dns.query
import dns.rcode
import dns.rdataclass
import dns.rdatatype
import dns.rdtypes.ANY.TXT
import dns.rdtypes.IN.A

from andai_intake.auth.dns import TXT_CHUNK, normalise
from andai_intake.testing.sign import read_zone

TTL = 1
UDP_DEFAULT_SIZE = 512
TCP_MAX = 65535
SERVED = {"TXT": dns.rdatatype.TXT, "A": dns.rdatatype.A}
PROBE_NAME = "probe.fake-dns.test"

log = logging.getLogger(__name__)


def _rdata(rdtype: str, value: str):
    if rdtype == "TXT":
        data = value.encode("ascii")
        chunks = [data[i : i + TXT_CHUNK] for i in range(0, len(data), TXT_CHUNK)] or [b""]
        return dns.rdtypes.ANY.TXT.TXT(dns.rdataclass.IN, dns.rdatatype.TXT, chunks)
    return dns.rdtypes.IN.A.A(dns.rdataclass.IN, dns.rdatatype.A, value)


def answer(query: dns.message.Message, zone_path: Path) -> dns.message.Message:
    """The response to `query` from the zone file as it is now."""
    response = dns.message.make_response(query)
    response.flags |= dns.flags.AA
    if len(query.question) != 1:
        response.set_rcode(dns.rcode.FORMERR)
        return response
    question = query.question[0]
    try:
        names = read_zone(zone_path)
    except (OSError, ValueError, TypeError):
        response.set_rcode(dns.rcode.SERVFAIL)
        return response
    records = names.get(normalise(question.name.to_text()))
    if records is None:
        response.set_rcode(dns.rcode.NXDOMAIN)
        return response
    rdtype = dns.rdatatype.to_text(question.rdtype)
    values = records.get(rdtype, []) if rdtype in SERVED else []
    if values:
        rrset = response.find_rrset(
            response.answer, question.name, dns.rdataclass.IN, question.rdtype, create=True
        )
        for value in values:
            rrset.add(_rdata(rdtype, value), TTL)
    return response


def to_wire(query: dns.message.Message, response: dns.message.Message, limit: int) -> bytes:
    """`response` in at most `limit` bytes; truncated (TC, no records) when it does not fit."""
    try:
        return response.to_wire(max_size=limit)
    except dns.exception.TooBig:
        short = dns.message.make_response(query)
        short.flags |= dns.flags.AA | dns.flags.TC
        return short.to_wire(max_size=limit)


def handle(wire: bytes, zone_path: Path, *, tcp: bool) -> bytes | None:
    try:
        query = dns.message.from_wire(wire)
    except dns.exception.DNSException:
        return None  # not DNS: no answer, as a resolver would see a timeout
    if tcp:
        limit = TCP_MAX
    elif query.edns >= 0:
        limit = max(UDP_DEFAULT_SIZE, min(query.payload, TCP_MAX))
    else:
        limit = UDP_DEFAULT_SIZE
    return to_wire(query, answer(query, zone_path), limit)


class _Udp(asyncio.DatagramProtocol):
    def __init__(self, zone_path: Path) -> None:
        self.zone_path = zone_path
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        reply = handle(data, self.zone_path, tcp=False)
        if reply is not None and self.transport is not None:
            self.transport.sendto(reply, addr)


async def _tcp_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, zone: Path):
    try:
        while True:
            head = await reader.readexactly(2)
            (length,) = struct.unpack("!H", head)
            reply = handle(await reader.readexactly(length), zone, tcp=True)
            if reply is None:
                break
            writer.write(struct.pack("!H", len(reply)) + reply)
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()


class FakeDns:
    """The server on one address: TCP and UDP on the same port (`port=0` picks a free one)."""

    def __init__(self, zone_path: Path, host: str = "127.0.0.1", port: int = 5353) -> None:
        self.zone_path = Path(zone_path)
        self.host = host
        self.port = port
        self._tcp: asyncio.base_events.Server | None = None
        self._udp: asyncio.DatagramTransport | None = None

    async def start(self) -> int:
        loop = asyncio.get_running_loop()
        self._tcp = await asyncio.start_server(
            lambda r, w: _tcp_client(r, w, self.zone_path), self.host, self.port
        )
        self.port = self._tcp.sockets[0].getsockname()[1]
        self._udp, _ = await loop.create_datagram_endpoint(
            lambda: _Udp(self.zone_path), local_addr=(self.host, self.port)
        )
        return self.port

    async def close(self) -> None:
        if self._udp is not None:
            self._udp.close()
        if self._tcp is not None:
            self._tcp.close()
            await self._tcp.wait_closed()


@contextlib.contextmanager
def running(zone_path: Path, host: str = "127.0.0.1", port: int = 0) -> Iterator[int]:
    """Serve in a background thread for the duration of the block; yields the port (tests)."""
    loop = asyncio.new_event_loop()
    server = FakeDns(zone_path, host, port)
    started = threading.Event()
    failed: list[Exception] = []

    def run() -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(server.start())
        except Exception as exc:  # noqa: BLE001 - handed to the caller
            failed.append(exc)
            started.set()
            return
        started.set()
        loop.run_forever()
        loop.run_until_complete(server.close())

    thread = threading.Thread(target=run, name="fake-dns", daemon=True)
    thread.start()
    started.wait(10)
    if failed:
        raise failed[0]
    try:
        yield server.port
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(10)
        loop.close()


def probe(host: str, port: int, timeout: float = 2.0) -> bool:
    """Whether a server on host:port answers a question (any rcode)."""
    query = dns.message.make_query(PROBE_NAME, "TXT")
    try:
        dns.query.udp(query, host, port=port, timeout=timeout)
    except (dns.exception.DNSException, OSError):
        return False
    return True


async def _serve(zone_path: Path, host: str, port: int) -> None:
    zone_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    server = FakeDns(zone_path, host, port)
    await server.start()
    log.info("fake-dns listening on %s:%d (udp+tcp)", host, server.port)
    await asyncio.Event().wait()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m andai_intake.testing.fake_dns")
    parser.add_argument("--zone", type=Path, default=Path("/tmp/fake-dns/zone.json"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5353)
    parser.add_argument(
        "--probe", action="store_true", help="ask a running server; exit 0 if it answers"
    )
    args = parser.parse_args(argv)
    if args.probe:
        return 0 if probe(args.host, args.port) else 1
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_serve(args.zone, args.host, args.port))
    return 0


if __name__ == "__main__":
    sys.exit(main())
