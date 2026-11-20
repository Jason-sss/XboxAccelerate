"""Tests for the DNS server wire format.

These build real DNS queries and parse the replies, so a malformed header
or resource record is caught here rather than in production.
"""

from __future__ import annotations

import asyncio
import socket
import struct

import pytest

from app.core.config import get_settings
from app.core.engine import AccelerationEngine
from app.core.host_mappings import HostMappingStore
from app.services.dns_server import DnsServer

HEADER = struct.Struct("!HHHHHH")


def build_query(hostname: str, qtype: int = 1, txid: int = 0x1234) -> bytes:
    """Build a standard recursive DNS query."""
    header = HEADER.pack(txid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(
        bytes([len(p)]) + p.encode("ascii") for p in hostname.strip(".").split(".")
    )
    return header + qname + b"\x00" + struct.pack("!HH", qtype, 1)


def parse_response(data: bytes) -> dict:
    """Parse a DNS response into a simple dict."""
    txid, flags, qdcount, ancount, _ns, _ar = HEADER.unpack(data[:12])
    answers = []
    offset = 12
    for _ in range(qdcount):
        while data[offset] != 0:
            offset += 1 + data[offset]
        offset += 1
        offset += 4

    for _ in range(ancount):
        if data[offset] & 0xC0 == 0xC0:
            offset += 2
        else:
            while data[offset] != 0:
                offset += 1 + data[offset]
            offset += 1
        rtype, rclass, ttl, rdlength = struct.unpack("!HHIH", data[offset : offset + 10])
        offset += 10
        rdata = data[offset : offset + rdlength]
        offset += rdlength
        ip = None
        if rtype == 1 and rdlength == 4:
            ip = socket.inet_ntop(socket.AF_INET, rdata)
        elif rtype == 28 and rdlength == 16:
            ip = socket.inet_ntop(socket.AF_INET6, rdata)
        answers.append({"type": rtype, "ttl": ttl, "ip": ip})

    return {
        "txid": txid,
        "rcode": flags & 0x000F,
        "qdcount": qdcount,
        "ancount": ancount,
        "answers": answers,
    }


class _QueryClient(asyncio.DatagramProtocol):
    """Sends one query and captures the reply."""

    def __init__(self) -> None:
        self.future: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if not self.future.done():
            self.future.set_result(data)


@pytest.fixture
def engine(tmp_path):
    settings = get_settings()
    settings.reset()
    mappings = HostMappingStore(tmp_path / "hosts.json")
    eng = AccelerationEngine(settings, mappings)
    eng.refresh()
    return eng


async def ask(port: int, query: bytes, timeout: float = 8.0) -> bytes:
    """Send a DNS query over UDP and return the raw reply."""
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        _QueryClient, remote_addr=("127.0.0.1", port)
    )
    try:
        transport.sendto(query)
        return await asyncio.wait_for(protocol.future, timeout=timeout)
    finally:
        transport.close()


@pytest.mark.asyncio
async def test_dns_returns_acceleration_address(engine):
    """A host covered by a rule resolves to the configured acceleration IP."""
    engine.settings.update({"xbox_global_ip": "23.64.58.45"})
    engine.refresh()

    dns = DnsServer(engine, "127.0.0.1", 15355)
    await dns.start()
    try:
        data = await ask(15355, build_query("assets1.xboxlive.com", 1))
    finally:
        await dns.stop()

    parsed = parse_response(data)
    assert parsed["txid"] == 0x1234
    assert parsed["rcode"] == 0, "expected NOERROR"
    assert parsed["ancount"] == 1
    assert parsed["answers"][0]["ip"] == "23.64.58.45"
    assert parsed["answers"][0]["type"] == 1
    assert parsed["answers"][0]["ttl"] == 60


@pytest.mark.asyncio
async def test_dns_manual_mapping_wins(engine):
    """A manual mapping takes priority over the built-in rules."""
    engine.host_mappings.add("assets1.xboxlive.com", "1.2.3.4")
    engine.settings.update({"xbox_global_ip": "23.64.58.45"})
    engine.refresh()

    dns = DnsServer(engine, "127.0.0.1", 15356)
    await dns.start()
    try:
        data = await ask(15356, build_query("assets1.xboxlive.com", 1))
    finally:
        await dns.stop()

    parsed = parse_response(data)
    assert parsed["answers"][0]["ip"] == "1.2.3.4"


@pytest.mark.asyncio
async def test_dns_blacklist_returns_zero(engine):
    """Blacklisted hosts resolve to 0.0.0.0."""
    dns = DnsServer(engine, "127.0.0.1", 15357)
    await dns.start()
    try:
        data = await ask(15357, build_query("level3.ssl.blizzard.com", 1))
    finally:
        await dns.stop()

    parsed = parse_response(data)
    assert parsed["ancount"] == 1
    assert parsed["answers"][0]["ip"] == "0.0.0.0"


@pytest.mark.asyncio
async def test_dns_unknown_host_returns_nxdomain(engine):
    """A host with no mapping and no upstream answer yields NXDOMAIN."""
    engine.settings.update(
        {"upstream_dns": [], "doh_servers": [], "allow_system_dns_fallback": False}
    )
    engine.refresh()

    dns = DnsServer(engine, "127.0.0.1", 15358)
    await dns.start()
    try:
        data = await ask(15358, build_query("nothing.invalid.example", 1))
    finally:
        await dns.stop()

    parsed = parse_response(data)
    assert parsed["rcode"] == 3, "expected NXDOMAIN"
    assert parsed["ancount"] == 0
    assert parsed["qdcount"] == 1, "the question section must be preserved"


@pytest.mark.asyncio
async def test_dns_over_tcp(engine):
    """The TCP transport answers the same way as UDP."""
    engine.settings.update({"xbox_global_ip": "23.64.58.45"})
    engine.refresh()

    dns = DnsServer(engine, "127.0.0.1", 15359)
    await dns.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", 15359)
        query = build_query("xvcf1.xboxlive.com", 1)
        writer.write(struct.pack("!H", len(query)) + query)
        await writer.drain()
        length = struct.unpack("!H", await reader.readexactly(2))[0]
        data = await reader.read(length)
        writer.close()
        await writer.wait_closed()
    finally:
        await dns.stop()

    parsed = parse_response(data)
    assert parsed["ancount"] == 1
    assert parsed["answers"][0]["ip"] == "23.64.58.45"


@pytest.mark.asyncio
async def test_dns_malformed_query_is_rejected(engine):
    """A truncated query must not crash the server."""
    dns = DnsServer(engine, "127.0.0.1", 15360)
    await dns.start()
    try:
        data = await ask(15360, b"\x00\x01")
    finally:
        await dns.stop()

    parsed = parse_response(data)
    assert parsed["rcode"] == 1, "expected FORMERR"
    assert parsed["qdcount"] == 0
