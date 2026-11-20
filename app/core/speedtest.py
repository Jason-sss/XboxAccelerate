"""Speed / latency measurement against acceleration addresses.

Ported from the C# ``SpeedTestService`` + ``HttpClientHelper``. The key
trick from the original is that the HTTP client connects to a *specific IP*
while keeping the original ``Host`` header and TLS SNI — that is what makes
it possible to measure one CDN edge in isolation.
"""

from __future__ import annotations

import asyncio
import ipaddress
import random
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx

#: Test targets, keyed by group. Each entry is ``(label, url)``.
#: Ported from ``SpeedTestDataBuilder.BuildSpeedTestFiles``.
SPEEDTEST_TARGETS: dict[str, list[tuple[str, str]]] = {
    "xbox_global": [
        ("Akamai-Xbox", "http://xvcf1.xboxlive.com/Z/routing/extraextralarge.txt"),
    ],
    "xbox_cn1": [
        (
            "XboxCn1-1",
            "http://assets1.xboxlive.cn/Z/routing/extraextralarge.txt",
        ),
        (
            "XboxCn1-2",
            "http://assets1.xboxlive.cn/8/71359f90-2c86-4118-8df8-8546ebd47956/"
            "0698b936-d300-4451-b9a0-0be0514bbbe5/1.4206.46191.0.88d41e86-b22b-49f0-"
            "bb08-9879d39f287d/Microsoft.254428597CFE2_1.4206.46191.0_neutral__"
            "8wekyb3d8bbwe_xs.xvc",
        ),
    ],
    "xbox_cn2": [
        (
            "XboxCn2-1",
            "http://dlassets.xboxlive.cn/public/content/0044aef3-bd7e-4478-8782-"
            "6191546dcd9a/f6585e19-765c-49c3-ba77-326c3766e05a/1.0.4.0.60281fce-b9c0-"
            "49f6-a2b6-027abad22637/ACBF_1.0.4.0_x64__b6krnev7r9sf8",
        ),
    ],
    "xbox_app": [
        (
            "XboxApp-1",
            "http://dl.delivery.mp.microsoft.com/Z/routing/extraextralarge.txt",
        ),
    ],
    "ps": [
        ("Ps-1", "http://gst.prod.dl.playstation.net/networktest/get_192m"),
    ],
    "akamai": [
        ("Akamai-Xbox", "http://xvcf1.xboxlive.com/Z/routing/extraextralarge.txt"),
        ("Akamai-Ps", "http://gst.prod.dl.playstation.net/networktest/get_192m"),
        ("Akamai-Ns", "http://ctest-dl-lp1.cdn.nintendo.net/30m"),
    ],
}

#: Fallback targets used when a group has no specific one.
DEFAULT_TARGETS = SPEEDTEST_TARGETS["akamai"]


@dataclass
class SpeedResult:
    """Outcome of measuring one acceleration address."""

    ip: str
    location: str = ""
    latency_ms: float | None = None
    speed_mib_s: float | None = None
    ttl: int | None = None
    bytes_downloaded: int = 0
    error: str | None = None
    ok: bool = False
    group: str = ""
    is_ipv6: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ip": self.ip,
            "location": self.location,
            "group": self.group,
            "is_ipv6": self.is_ipv6,
            "latency_ms": self.latency_ms,
            "speed_mib_s": self.speed_mib_s,
            "ttl": self.ttl,
            "bytes_downloaded": self.bytes_downloaded,
            "error": self.error,
            "ok": self.ok,
        }


@dataclass
class _Target:
    label: str
    url: str
    host: str
    port: int
    is_https: bool
    path: str = "/"


@dataclass
class _Stats:
    """Accumulates download statistics for one measurement."""

    total_bytes: int = 0
    speed_mib_s: float = 0.0
    latency_ms: float | None = None
    error: str | None = None
    headers_ok: bool = False
    samples: list[float] = field(default_factory=list)


def _parse_target(url: str, label: str = "") -> _Target:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    # The request target keeps its leading slash ("/path?query").
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return _Target(
        label=label or url,
        url=url,
        host=host,
        port=port,
        is_https=parts.scheme == "https",
        path=path,
    )


async def _measure_one(
    ip: str,
    target: _Target,
    timeout: float,
    chunk_bytes: int,
    user_agent: str = "XboxDownload",
) -> _Stats:
    """Download a byte range from ``target`` pinned to ``ip``.

    Measures the time-to-first-byte (latency) and the sustained transfer
    rate over the fetched range.
    """
    stats = _Stats()
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        stats.error = f"Invalid IP: {ip}"
        return stats

    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    range_from = random.randint(0, max(0, 30 * 1024 * 1024 - chunk_bytes))
    range_to = range_from + chunk_bytes - 1

    ssl_context = ssl.create_default_context()
    if target.is_https:
        # The upstream certificate is validated against the hostname, not the
        # pinned IP — same semantics as the C# original.
        ssl_context.check_hostname = True

    start = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                host=ip,
                port=target.port,
                family=family,
                ssl=ssl_context if target.is_https else None,
                server_hostname=target.host if target.is_https else None,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        stats.error = "连接超时"
        return stats
    except (OSError, ssl.SSLError) as exc:
        stats.error = f"连接失败: {exc}"
        return stats

    try:
        # The request target must keep its leading slash: "GET /path HTTP/1.1".
        request = (
            f"GET {target.path} HTTP/1.1\r\n"
            f"Host: {target.host}\r\n"
            f"User-Agent: {user_agent}\r\n"
            f"Range: bytes={range_from}-{range_to}\r\n"
            f"Accept: */*\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("latin-1", "replace")
        writer.write(request)
        await writer.drain()

        headers_raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=timeout)
        stats.latency_ms = (time.perf_counter() - start) * 1000
        status_line = headers_raw.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        stats.headers_ok = " 2" in status_line or " 3" in status_line
        if not stats.headers_ok:
            stats.error = f"HTTP {status_line.strip()}"

        # Skip any body bytes already buffered with the headers.
        body_start = headers_raw.rfind(b"\r\n\r\n")
        already = len(headers_raw) - (body_start + 4)
        if already > 0:
            stats.total_bytes += already

        while True:
            chunk = await asyncio.wait_for(reader.read(65536), timeout=timeout)
            if not chunk:
                break
            stats.total_bytes += len(chunk)
            elapsed = time.perf_counter() - start
            if elapsed > 0.1:
                stats.speed_mib_s = stats.total_bytes / (1048576 * elapsed)
            if stats.total_bytes >= chunk_bytes:
                break
    except asyncio.TimeoutError:
        stats.error = stats.error or "读取超时"
    except (OSError, ssl.SSLError, asyncio.IncompleteReadError) as exc:
        stats.error = stats.error or f"传输失败: {exc}"
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except (OSError, asyncio.CancelledError):
            pass

    if stats.total_bytes > 0 and stats.speed_mib_s <= 0:
        elapsed = max(time.perf_counter() - start, 0.001)
        stats.speed_mib_s = stats.total_bytes / (1048576 * elapsed)
    return stats


async def _measure_ttl(ip: str, timeout: float = 1.0) -> int | None:
    """Best-effort TTL lookup.

    Uses the system resolver where possible; a raw socket ``recvmsg``
    with ``IP_TTL`` is used as a fallback on Linux.
    """
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None

    if address.version == 4:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.settimeout(timeout)
                sock.connect((ip, 9))
                ttl = sock.getsockopt(socket.IPPROTO_IP, socket.IP_TTL)
                return int(ttl)
            finally:
                sock.close()
        except OSError:
            return None

    # IPv6: hop limit via the ancillary data of recvmsg.
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        try:
            sock.settimeout(timeout)
            sock.connect((ip, 9))
            sock.send(b"\x00")
            data, ancdata, _flags, _addr = sock.recvmsg(64, 256)
            for level, opttype, value in ancdata:
                if level == socket.IPPROTO_IPV6 and opttype == socket.IPV6_HOPLIMIT:
                    return int(value[0])
            _ = data
        finally:
            sock.close()
    except OSError:
        return None
    return None


async def measure_addresses(
    ips: Iterable[str],
    target_url: str,
    *,
    timeout: float = 10.0,
    chunk_bytes: int = 10 * 1024 * 1024,
    concurrency: int = 8,
    locations: dict[str, str] | None = None,
    group: str = "",
    with_ttl: bool = True,
) -> list[SpeedResult]:
    """Measure a batch of acceleration addresses concurrently."""
    ip_list = [ip for ip in dict.fromkeys(ips) if ip]
    if not ip_list:
        return []

    target = _parse_target(target_url)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    locations = locations or {}

    async def worker(ip: str) -> SpeedResult:
        async with semaphore:
            try:
                address = ipaddress.ip_address(ip)
            except ValueError:
                return SpeedResult(
                    ip=ip,
                    group=group,
                    error=f"Invalid IP: {ip}",
                    ok=False,
                )
            stats, ttl = await asyncio.gather(
                _measure_one(ip, target, timeout, chunk_bytes),
                _measure_ttl(ip) if with_ttl else asyncio.sleep(0, result=None),
            )
            return SpeedResult(
                ip=ip,
                location=locations.get(ip, ""),
                group=group,
                is_ipv6=address.version == 6,
                latency_ms=round(stats.latency_ms, 2) if stats.latency_ms else None,
                speed_mib_s=round(stats.speed_mib_s, 3),
                ttl=ttl,
                bytes_downloaded=stats.total_bytes,
                error=stats.error,
                ok=stats.total_bytes > 0,
            )

    results = await asyncio.gather(*(worker(ip) for ip in ip_list))
    # Fastest first: reachable before unreachable, then by speed, then latency.
    results.sort(
        key=lambda r: (
            not r.ok,
            -(r.speed_mib_s or 0),
            (r.latency_ms if r.latency_ms is not None else 1e9),
        )
    )
    return results


async def pick_fastest(
    ips: Iterable[str],
    target_url: str,
    *,
    timeout: float = 10.0,
    chunk_bytes: int = 10 * 1024 * 1024,
    concurrency: int = 8,
) -> str | None:
    """Measure the given addresses and return the fastest working one.

    Mirrors ``SpeedTestService.FindFastestOrBestAkamaiIpAsync``: the first
    address that successfully serves bytes wins; otherwise the one that
    transferred the most bytes wins.
    """
    ip_list = [ip for ip in dict.fromkeys(ips) if ip]
    if not ip_list:
        return None
    if len(ip_list) == 1:
        return ip_list[0]

    target = _parse_target(target_url)

    async def attempt(ip: str) -> tuple[str, int]:
        stats = await _measure_one(ip, target, timeout, chunk_bytes)
        return ip, stats.total_bytes

    # Race all of them; the first to deliver bytes wins.
    tasks = [asyncio.create_task(attempt(ip)) for ip in ip_list]
    done_count = 0
    try:
        for coro in asyncio.as_completed(tasks):
            ip, downloaded = await coro
            done_count += 1
            if downloaded > 0:
                for task in tasks:
                    task.cancel()
                return ip
        _ = done_count
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    return None


async def http_latency_ms(
    url: str, ip: str, timeout: float = 5.0
) -> float | None:
    """Measure the time-to-first-byte of a URL pinned to a specific IP."""
    target = _parse_target(url)
    stats = await _measure_one(ip, target, timeout, 64 * 1024)
    return stats.latency_ms if stats.headers_ok else None


async def fastest_https_ip(
    ips: Iterable[str], port: int = 443, timeout: float = 3.0
) -> str | None:
    """Return the first IP that completes a TLS handshake fastest.

    Ported from ``HttpClientHelper.GetFastestHttpsIpAsync``.
    """
    ip_list = [ip for ip in dict.fromkeys(ips) if ip]
    if not ip_list:
        return None

    async def attempt(ip: str) -> str | None:
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return None
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    host=ip, port=port, family=family, ssl=ssl.create_default_context()
                ),
                timeout=timeout,
            )
        except (asyncio.TimeoutError, OSError, ssl.SSLError):
            return None
        try:
            writer.close()
            await writer.wait_closed()
        except (OSError, asyncio.CancelledError):
            pass
        return ip

    tasks = [asyncio.create_task(attempt(ip)) for ip in ip_list]
    try:
        for coro in asyncio.as_completed(tasks):
            result = await coro
            if result:
                return result
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return None


async def fetch_json(url: str, timeout: float = 15.0) -> Any:
    """Fetch a JSON document (used for MS Store metadata)."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(
            url, headers={"User-Agent": "XboxDownload-Web/1.0"}
        )
        response.raise_for_status()
        return response.json()
