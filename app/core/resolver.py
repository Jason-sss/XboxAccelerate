"""Upstream resolution: plain DNS and DNS-over-HTTPS.

The engine never resolves a hostname itself for the acceleration path; it
asks an upstream resolver and applies the acceleration mapping on top. This
module isolates that "ask upstream" step.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import random
import socket
import struct
from typing import Iterable

import httpx

#: DNS record types we care about.
TYPE_A = 1
TYPE_AAAA = 28

#: Minimal DNS header layout: id, flags, qdcount, ancount, nscount, arcount.
_HEADER = struct.Struct("!HHHHHH")

#: Resource-record layout: name, type, class, ttl, rdlength.
_RR = struct.Struct("!HHHIH")


class _DnsProtocol(asyncio.DatagramProtocol):
    """Collects exactly one datagram reply."""

    def __init__(self, future: asyncio.Future[bytes]) -> None:
        self._future = future

    def datagram_received(self, data: bytes, addr: object) -> None:
        if not self._future.done():
            self._future.set_result(data)

    def error_received(self, exc: Exception) -> None:
        if not self._future.done():
            self._future.set_exception(exc)


class UpstreamResolver:
    """Resolves hostnames via UDP DNS and DoH."""

    def __init__(
        self,
        upstream_dns: Iterable[str] | None = None,
        doh_servers: Iterable[dict] | None = None,
        timeout: float = 5.0,
        allow_system_fallback: bool = True,
    ) -> None:
        self.upstream_dns: list[str] = list(upstream_dns or [])
        self.doh_servers: list[dict] = list(doh_servers or [])
        self.timeout = timeout
        self.allow_system_fallback = allow_system_fallback

    # -- plain DNS ---------------------------------------------------------
    async def resolve_dns(
        self, hostname: str, qtype: int = TYPE_A, server: str | None = None
    ) -> list[str]:
        """Resolve ``hostname`` using UDP DNS against ``server``.

        Returns a list of IP strings; empty on any failure.
        """
        servers = [server] if server else self.upstream_dns
        if not servers:
            return []

        query = self._build_query(hostname, qtype)
        for target in servers:
            try:
                return await self._query_udp(target, query, qtype)
            except (OSError, asyncio.TimeoutError, struct.error):
                continue
        return []

    async def _query_udp(self, server: str, query: bytes, qtype: int) -> list[str]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bytes] = loop.create_future()
        family = socket.AF_INET6 if ":" in server else socket.AF_INET
        transport, _protocol = await asyncio.wait_for(
            loop.create_datagram_endpoint(
                lambda: _DnsProtocol(future),
                remote_addr=(server, 53),
                family=family,
            ),
            timeout=self.timeout,
        )
        try:
            transport.sendto(query)
            response = await asyncio.wait_for(future, timeout=self.timeout)
        finally:
            transport.close()
        return self._parse_answers(response, qtype)

    @staticmethod
    def _build_query(hostname: str, qtype: int) -> bytes:
        txid = random.randint(0, 0xFFFF)
        flags = 0x0100  # RD
        header = _HEADER.pack(txid, flags, 1, 0, 0, 0)
        qname = b"".join(
            bytes([len(part)]) + part.encode("ascii")
            for part in hostname.strip(".").split(".")
            if part
        )
        return header + qname + b"\x00" + struct.pack("!HH", qtype, 1)

    @staticmethod
    def _parse_answers(response: bytes, qtype: int) -> list[str]:
        if len(response) < 12:
            return []
        _, flags, qdcount, ancount = _HEADER.unpack(response[:12])[:4]
        if flags & 0x000F:  # rcode != NOERROR
            return []
        offset = 12
        for _ in range(qdcount):
            offset = _skip_name(response, offset)
            offset += 4  # qtype + qclass
            if offset > len(response):
                return []
        results: list[str] = []
        for _ in range(ancount):
            offset = _skip_name(response, offset)
            if offset + 12 > len(response):
                break
            _, _, _, _, rdlength = _RR.unpack(response[offset : offset + 12])
            offset += 12
            rdata = response[offset : offset + rdlength]
            offset += rdlength
            if qtype == TYPE_A and rdlength == 4:
                results.append(socket.inet_ntop(socket.AF_INET, rdata))
            elif qtype == TYPE_AAAA and rdlength == 16:
                results.append(socket.inet_ntop(socket.AF_INET6, rdata))
        return results

    # -- DNS over HTTPS ----------------------------------------------------
    async def resolve_doh(
        self, hostname: str, qtype: int = TYPE_A, server: dict | None = None
    ) -> list[str]:
        """Resolve ``hostname`` via DNS-over-HTTPS."""
        servers = [server] if server else self.doh_servers
        if not servers:
            return []
        question = "A" if qtype == TYPE_A else "AAAA"
        for cfg in servers:
            url = str(cfg.get("url") or "").strip()
            if not url:
                continue
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout, verify=True, follow_redirects=False
                ) as client:
                    response = await client.get(
                        url,
                        params={"name": hostname, "type": question},
                        headers={
                            "Accept": "application/dns-json",
                            "User-Agent": "XboxDownload-Web/1.0",
                        },
                    )
                    if response.status_code != 200:
                        continue
                    payload = response.json()
            except (httpx.HTTPError, ValueError, json.JSONDecodeError):
                continue
            answers = payload.get("Answer") or []
            results: list[str] = []
            for answer in answers:
                if not isinstance(answer, dict):
                    continue
                data = str(answer.get("data") or "")
                try:
                    address = ipaddress.ip_address(data)
                except ValueError:
                    continue
                if qtype == TYPE_A and address.version == 4:
                    results.append(str(address))
                elif qtype == TYPE_AAAA and address.version == 6:
                    results.append(str(address))
            if results:
                return results
        return []

    async def resolve(
        self, hostname: str, qtype: int = TYPE_A, prefer_doh: bool = False
    ) -> list[str]:
        """Resolve using DoH first (if asked) then plain DNS."""
        if prefer_doh and self.doh_servers:
            results = await self.resolve_doh(hostname, qtype)
            if results:
                return results
        return await self.resolve_dns(hostname, qtype)

    async def resolve_system(self, hostname: str, qtype: int = TYPE_A) -> list[str]:
        """Resolve via the operating system resolver (getaddrinfo).

        This is the last-resort fallback. Many container networks block
        outbound UDP/53 while the system resolver still works, so it keeps
        the acceleration service functional in restricted environments.
        """
        family = socket.AF_INET6 if qtype == TYPE_AAAA else socket.AF_INET
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                hostname,
                None,
                family=family,
                type=socket.SOCK_STREAM,
            )
        except (socket.gaierror, OSError, UnicodeError, TypeError):
            return []
        results: list[str] = []
        for info in infos:
            address = info[4][0]
            if address and address not in results:
                results.append(address)
        return results

    async def resolve_all(self, hostname: str, want_ipv6: bool = False) -> list[str]:
        """Resolve A (and optionally AAAA) records, de-duplicated.

        Tries, in order: the UDP upstream servers, DoH, then the system
        resolver. Restricted container networks often block outbound UDP/53
        while HTTPS still works, so the fallbacks matter.
        """
        results = await self.resolve_dns(hostname, TYPE_A)
        if not results:
            results = await self.resolve_doh(hostname, TYPE_A)
        if not results and self.allow_system_fallback:
            results = await self.resolve_system(hostname, TYPE_A)
        if want_ipv6:
            v6 = await self.resolve_dns(hostname, TYPE_AAAA)
            if not v6:
                v6 = await self.resolve_doh(hostname, TYPE_AAAA)
            if not v6 and self.allow_system_fallback:
                v6 = await self.resolve_system(hostname, TYPE_AAAA)
            results += v6
        seen: set[str] = set()
        unique: list[str] = []
        for item in results:
            if item not in seen:
                seen.add(item)
                unique.append(item)
        return unique


def _skip_name(buffer: bytes, offset: int) -> int:
    """Skip over a (possibly compressed) DNS name, returning the new offset."""
    while offset < len(buffer):
        length = buffer[offset]
        if length == 0:
            return offset + 1
        if length & 0xC0 == 0xC0:
            return offset + 2
        offset += 1 + length
    return offset
