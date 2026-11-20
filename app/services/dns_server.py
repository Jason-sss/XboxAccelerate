"""A DNS server that applies the acceleration mapping.

Listens on UDP (and optionally TCP) and answers queries using the
acceleration engine. This is the container-friendly replacement for the
Windows-only "set local DNS" feature of the desktop app.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
from typing import Any

from app.core.engine import AccelerationEngine
from app.core.resolver import TYPE_A, TYPE_AAAA

log = logging.getLogger(__name__)

_HEADER = struct.Struct("!HHHHHH")
#: Resource-record layout: name, type, class, ttl, rdlength.
_RR = struct.Struct("!HHHIH")
MAX_UDP = 512


class DnsServer:
    """Minimal DNS server backed by the acceleration engine."""

    def __init__(self, engine: AccelerationEngine, host: str = "0.0.0.0", port: int = 53) -> None:
        self.engine = engine
        self.host = host
        self.port = port
        self._transport: asyncio.DatagramTransport | None = None
        self._tcp_server: asyncio.AbstractServer | None = None
        self._running = False
        self.query_count = 0

    async def start(self) -> None:
        if self._running:
            return
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _DnsHandler(self), local_addr=(self.host, self.port)
        )
        # asyncio.start_server is the stream-oriented API: it calls the
        # callback with (reader, writer) for every accepted connection.
        self._tcp_server = await asyncio.start_server(
            self._handle_tcp, host=self.host, port=self.port
        )
        self._running = True
        log.info("DNS server listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        self._running = False
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        if self._tcp_server is not None:
            self._tcp_server.close()
            await self._tcp_server.wait_closed()
            self._tcp_server = None
        log.info("DNS server stopped")

    @property
    def running(self) -> bool:
        return self._running

    # -- TCP ---------------------------------------------------------------
    async def _handle_tcp(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            length_bytes = await reader.readexactly(2)
            (length,) = struct.unpack("!H", length_bytes)
            query = await reader.readexactly(length)
            response = await self._build_response(query)
            writer.write(struct.pack("!H", len(response)) + response)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (OSError, asyncio.CancelledError):
                pass

    # -- response building -------------------------------------------------
    async def _build_response(self, query: bytes) -> bytes:
        self.query_count += 1
        if len(query) < 12:
            return self._header_only(query, rcode=1)

        txid, _flags, qdcount = _HEADER.unpack(query[:12])[:3]
        if qdcount < 1:
            return self._header_only(query, rcode=1)

        try:
            hostname, qtype, offset = self._parse_question(query)
        except (ValueError, struct.error, IndexError):
            return self._header_only(query, rcode=1)

        question = query[12:offset]
        rcode = 0
        answers: list[bytes] = []

        resolution = await self.engine.resolve(hostname, qtype)
        ips = resolution.ips

        # Select the records that match the question type.
        if qtype == TYPE_AAAA:
            selected = [ip for ip in ips if ":" in ip]
        elif qtype == TYPE_A:
            selected = [ip for ip in ips if ":" not in ip]
        else:
            selected = list(ips)

        for ip in selected:
            is_v6 = ":" in ip
            try:
                address = socket.inet_pton(
                    socket.AF_INET6 if is_v6 else socket.AF_INET, ip
                )
            except OSError:
                continue
            record_type = TYPE_AAAA if is_v6 else TYPE_A
            answers.append(
                _RR.pack(0xC00C, record_type, 0x0001, 60, len(address)) + address
            )
        if not answers:
            rcode = 3  # NXDOMAIN
            # Keep the question section so the client can parse the reply.
            header = _HEADER.pack(txid, 0x8180 | rcode, qdcount, 0, 0, 0)
            return header + question

        header = _HEADER.pack(txid, 0x8180 | rcode, qdcount, len(answers), 0, 0)
        return header + question + b"".join(answers)

    @staticmethod
    def _header_only(query: bytes, rcode: int) -> bytes:
        txid = struct.unpack("!H", query[:2])[0] if len(query) >= 2 else 0
        return _HEADER.pack(txid, 0x8180 | rcode, 0, 0, 0, 0)

    @staticmethod
    def _parse_question(query: bytes) -> tuple[str, int, int]:
        offset = 12
        labels: list[str] = []
        while offset < len(query):
            length = query[offset]
            if length == 0:
                offset += 1
                break
            if length & 0xC0 == 0xC0:
                offset += 2
                break
            labels.append(query[offset + 1 : offset + 1 + length].decode("ascii", "replace"))
            offset += 1 + length
        if offset + 4 > len(query):
            raise ValueError("truncated question")
        qtype, _qclass = struct.unpack("!HH", query[offset : offset + 4])
        offset += 4
        return ".".join(labels), qtype, offset


class _DnsHandler(asyncio.DatagramProtocol):
    """Handles incoming UDP DNS queries."""

    def __init__(self, server: DnsServer) -> None:
        self.server = server
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: Any) -> None:
        asyncio.create_task(self._respond(data, addr))

    async def _respond(self, data: bytes, addr: Any) -> None:
        try:
            response = await self.server._build_response(data)
        except Exception:  # pragma: no cover - defensive
            log.exception("DNS query handling failed")
            response = self.server._header_only(data, rcode=2)
        if self.transport is not None:
            try:
                self.transport.sendto(response, addr)
            except OSError:
                pass
