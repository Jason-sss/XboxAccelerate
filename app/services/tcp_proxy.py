"""TCP listeners that implement the HTTP/HTTPS acceleration.

The desktop app ran two raw TCP listeners (port 80 and 443) and inspected
the traffic itself. In a container the same idea is implemented as:

* **HTTP listener** — parses the request line and ``Host`` header, then
  either redirects to an acceleration host or relays the request to the
  pinned acceleration IP.
* **HTTPS listener** — terminates TLS with a locally generated certificate,
  reads the SNI/``Host``, then opens a *second* TLS session to the pinned
  acceleration IP with the original SNI and relays bytes both ways.

This reproduces the "connect to a specific IP while keeping the original
Host/SNI" trick that makes CDN-edge acceleration work.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from dataclasses import dataclass, field
from typing import Any

from app.core import host_rules
from app.core.cert_rules import CertificatePolicy, CertDomainMap, insecure_client_context
from app.core.engine import AccelerationEngine

log = logging.getLogger(__name__)

RELAY_CHUNK = 65536
IDLE_TIMEOUT = 300.0


@dataclass
class ProxyStats:
    """Counters exposed through the API."""

    http_connections: int = 0
    https_connections: int = 0
    relayed_connections: int = 0
    redirected_requests: int = 0
    failed_connections: int = 0
    bytes_to_client: int = 0
    bytes_to_upstream: int = 0
    started_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "http_connections": self.http_connections,
            "https_connections": self.https_connections,
            "relayed_connections": self.relayed_connections,
            "redirected_requests": self.redirected_requests,
            "failed_connections": self.failed_connections,
            "bytes_to_client": self.bytes_to_client,
            "bytes_to_upstream": self.bytes_to_upstream,
            "uptime_s": round(time.time() - self.started_at, 1),
        }


@dataclass
class _RequestHead:
    """The head of an HTTP request plus any bytes already read past it."""

    head: bytes
    extra: bytes
    method: str = ""
    path: str = ""
    host: str = ""


def _parse_request_head(raw: bytes) -> _RequestHead | None:
    """Split the request head from the body bytes that followed it."""
    separator = raw.find(b"\r\n\r\n")
    terminator = 4
    if separator < 0:
        separator = raw.find(b"\n\n")
        terminator = 2
        if separator < 0:
            return None
    head = raw[:separator]
    extra = raw[separator + terminator :]

    lines = head.replace(b"\r\n", b"\n").split(b"\n")
    if not lines or not lines[0]:
        return None
    parts = lines[0].decode("latin-1", "replace").split()
    if len(parts) < 2:
        return None
    method, path = parts[0], parts[1]

    host = ""
    for line in lines[1:]:
        text = line.decode("latin-1", "replace")
        if text.lower().startswith("host:"):
            host = text.split(":", 1)[1].strip()
            break
    return _RequestHead(head=head + b"\r\n\r\n", extra=extra, method=method, path=path, host=host.lower())


class TcpProxyService:
    """HTTP/HTTPS acceleration listeners."""

    def __init__(
        self,
        engine: AccelerationEngine,
        host: str = "0.0.0.0",
        http_port: int = 80,
        https_port: int = 443,
        certificate_provider: Any | None = None,
        cert_domains: CertDomainMap | None = None,
    ) -> None:
        self.engine = engine
        self.host = host
        self.http_port = http_port
        self.https_port = https_port
        self.certificate_provider = certificate_provider
        self.cert_domains = cert_domains or CertDomainMap()
        self.stats = ProxyStats()
        self._http_server: asyncio.AbstractServer | None = None
        self._https_server: asyncio.AbstractServer | None = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        if self._running:
            return
        loop = asyncio.get_running_loop()

        self._http_server = await asyncio.start_server(
            self._handle_http, host=self.host, port=self.http_port
        )
        self._https_server = await asyncio.start_server(
            self._handle_https, host=self.host, port=self.https_port
        )
        self._running = True
        log.info("HTTP proxy on %s:%s, HTTPS proxy on %s:%s",
                 self.host, self.http_port, self.host, self.https_port)

    async def stop(self) -> None:
        self._running = False
        for server in (self._http_server, self._https_server):
            if server is not None:
                server.close()
                await server.wait_closed()
        self._http_server = None
        self._https_server = None
        log.info("TCP proxy stopped")

    # -- HTTP --------------------------------------------------------------
    async def _handle_http(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.stats.http_connections += 1
        peer = _peer(writer)
        try:
            raw = await asyncio.wait_for(reader.read(16384), timeout=30)
            request = _parse_request_head(raw)
            if request is None or not request.host:
                await _write_http(writer, 400, b"Bad Request")
                return

            target = await self._resolve_target(request.host)
            if target is None:
                await _write_http(writer, 404, b"File not found.")
                return

            if target.redirect:
                self.stats.redirected_requests += 1
                location = f"http://{target.host}{request.path}"
                await _write_redirect(writer, location)
                log.info("redirect %s -> %s (%s)", request.host, target.host, peer)
                return

            # Relay to the pinned acceleration IP.
            await self._relay_http(reader, writer, request, target.ip)
        except asyncio.TimeoutError:
            self.stats.failed_connections += 1
        except (ConnectionError, OSError) as exc:
            self.stats.failed_connections += 1
            log.debug("HTTP proxy connection failed (%s): %s", peer, exc)
        except Exception:
            self.stats.failed_connections += 1
            log.exception("HTTP proxy error for %s", peer)
        finally:
            await _close(writer)

    async def _relay_http(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        request: _RequestHead,
        ip: str,
    ) -> None:
        """Relay a plain HTTP request to the pinned IP."""
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(host=ip, port=80), timeout=10
            )
        except (asyncio.TimeoutError, OSError) as exc:
            self.stats.failed_connections += 1
            await _write_http(writer, 502, f"Bad Gateway: {exc}".encode())
            return

        self.stats.relayed_connections += 1
        try:
            # Rewrite the request so the Host header points at the real host
            # but the connection is pinned to the acceleration IP.
            upstream_writer.write(request.head + request.extra)
            await upstream_writer.drain()
            await self._relay_both(reader, writer, upstream_reader, upstream_writer)
        finally:
            await _close(upstream_writer)

    async def _relay_both(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        """Copy bytes in both directions until either side finishes.

        ``asyncio.gather`` would wait for BOTH pumps, but the
        client->upstream pump blocks forever once the server has closed
        its half of the connection. Waiting on the first pump to finish
        and then tearing both sides down is what keeps the relay from
        hanging.
        """
        to_upstream = asyncio.create_task(
            self._pump(client_reader, upstream_writer, to_upstream=True)
        )
        to_client = asyncio.create_task(
            self._pump(upstream_reader, client_writer, to_upstream=False)
        )
        try:
            done, pending = await asyncio.wait(
                {to_upstream, to_client}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            # Surface any exception raised by the finished pump.
            for task in done:
                exc = task.exception()
                if exc is not None:
                    raise exc
        finally:
            for task in (to_upstream, to_client):
                if not task.done():
                    task.cancel()
            await asyncio.gather(to_upstream, to_client, return_exceptions=True)
            # Half-close both sides so the peer sees EOF promptly.
            for writer in (client_writer, upstream_writer):
                try:
                    if writer.can_write_eof():
                        writer.write_eof()
                except (OSError, RuntimeError):
                    pass

    # -- HTTPS -------------------------------------------------------------
    async def _handle_https(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.stats.https_connections += 1
        peer = _peer(writer)
        try:
            server_ctx = self._server_ssl_context()
            if server_ctx is None:
                await _close(writer)
                return
            try:
                await writer.start_tls(server_ctx)
            except (ssl.SSLError, ConnectionError, OSError):
                self.stats.failed_connections += 1
                return

            raw = await asyncio.wait_for(reader.read(16384), timeout=30)
            request = _parse_request_head(raw)
            if request is None or not request.host:
                await _write_http(writer, 400, b"Bad Request")
                return

            target = await self._resolve_target(request.host)
            if target is None:
                await _write_http(writer, 404, b"File not found.")
                return

            if target.redirect:
                self.stats.redirected_requests += 1
                await _write_redirect(writer, f"https://{target.host}{request.path}")
                return

            await self._relay_https(reader, writer, request, target.ip, target.host)
        except (asyncio.TimeoutError, ConnectionError, OSError):
            self.stats.failed_connections += 1
        except Exception:  # pragma: no cover - defensive
            log.exception("HTTPS handling failed for %s", peer)
            self.stats.failed_connections += 1
        finally:
            await _close(writer)

    async def _relay_https(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        request: _RequestHead,
        ip: str,
        sni_host: str,
    ) -> None:
        """Relay a TLS request to the pinned IP, keeping the original SNI."""
        client_ctx = insecure_client_context()
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(
                    host=ip,
                    port=443,
                    ssl=client_ctx,
                    server_hostname=sni_host,
                ),
                timeout=10,
            )
        except (asyncio.TimeoutError, OSError, ssl.SSLError) as exc:
            self.stats.failed_connections += 1
            await _write_http(writer, 502, f"Bad Gateway: {exc}".encode())
            return

        # The upstream certificate is verified manually: a pinned CDN edge
        # often presents a certificate for a related hostname.
        policy = CertificatePolicy(
            target_host=sni_host,
            expected_hosts=self.cert_domains.expected_hosts(sni_host),
        )
        ok, dns_names = policy.verify(der=_peer_certificate(upstream_writer))
        if not ok:
            self.stats.failed_connections += 1
            message = policy.error_message(dns_names)
            log.warning("certificate check failed for %s: %s", sni_host, message)
            await _close(upstream_writer)
            await _write_http(writer, 502, message.encode())
            return

        self.stats.relayed_connections += 1
        try:
            upstream_writer.write(request.head + request.extra)
            await upstream_writer.drain()
            await self._relay_both(reader, writer, upstream_reader, upstream_writer)
        finally:
            await _close(upstream_writer)

    # -- pumping -----------------------------------------------------------
    async def _pump(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        to_upstream: bool,
    ) -> None:
        """Copy bytes between two streams until EOF or idle timeout."""
        total = 0
        while True:
            try:
                data = await asyncio.wait_for(reader.read(RELAY_CHUNK), timeout=IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                break
            if not data:
                break
            writer.write(data)
            try:
                await writer.drain()
            except (ConnectionError, OSError):
                break
            total += len(data)
            if to_upstream:
                self.stats.bytes_to_upstream += len(data)
            else:
                self.stats.bytes_to_client += len(data)
        _ = total

    # -- target selection --------------------------------------------------
    async def _resolve_target(self, host: str) -> "_Target | None":
        """Decide where a request for ``host`` should go.

        The international Xbox download hosts are redirected to their
        domestic (``.cn``) equivalents first — that is the whole point of
        the accelerator: the download must be served by domestic CDN
        nodes instead of the international ones.

        A manual host mapping wins over the redirect, so an operator can
        still pin a specific host explicitly.
        """
        host = host_rules.normalise_host(host)

        # 1. An explicit manual mapping always wins.
        resolution = await self.engine.resolve(host)
        if resolution.source == "manual" and resolution.ips:
            ip = resolution.ips[0]
            if ip and ip != "0.0.0.0":
                return _Target(ip=ip, host=host, redirect=False)

        # 2. International Xbox hosts are redirected to the domestic ones.
        cn_host = host_rules.cn_redirect_target(
            host, self.engine.game_links_shown
        )
        if cn_host:
            return _Target(ip="", host=cn_host, redirect=True)

        # 3. Otherwise use the acceleration address of the service group.
        if resolution.source == "rule" and resolution.ips:
            ip = resolution.ips[0]
            if ip and ip != "0.0.0.0":
                return _Target(ip=ip, host=host, redirect=False)

        # 4. Known redirect-only hosts (Battle.net CDN aliases).
        new_host = _redirect_for(host)
        if new_host:
            return _Target(ip="", host=new_host, redirect=True)
        return None

    def _server_ssl_context(self) -> ssl.SSLContext | None:
        if self.certificate_provider is None:
            return None
        try:
            cert_path, key_path = self.certificate_provider.certificate_files()
        except Exception:  # pragma: no cover - defensive
            log.exception("failed to obtain server certificate")
            return None
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        except (OSError, ssl.SSLError):
            log.exception("failed to load the server certificate")
            return None
        return context


@dataclass
class _Target:
    ip: str
    host: str
    redirect: bool


#: Redirect-only hosts: the Battle.net CDN aliases that have no
#: acceleration group of their own. The Microsoft/Xbox entries live in
#: ``host_rules.XBOX_CN_REDIRECTS`` — one table per concern.
_BATTLE_REDIRECTS: dict[str, str] = {
    "us.cdn.blizzard.com": "blzddist1-a.akamaihd.net",
    "eu.cdn.blizzard.com": "blzddist1-a.akamaihd.net",
    "kr.cdn.blizzard.com": "blzddist1-a.akamaihd.net",
    "level3.blizzard.com": "blzddist1-a.akamaihd.net",
    "blizzard.gcdn.cloudn.co.kr": "blzddist1-a.akamaihd.net",
}


def _redirect_for(host: str) -> str | None:
    """Return the regional equivalent for well-known hosts, if any."""
    return _BATTLE_REDIRECTS.get(host)


async def _write_http(
    writer: asyncio.StreamWriter, status: int, body: bytes, content_type: str = "text/html"
) -> None:
    reason = {400: "Bad Request", 404: "Not Found", 502: "Bad Gateway"}.get(
        status, "OK"
    )
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode("ascii")
    try:
        writer.write(head + body)
        await writer.drain()
    except (ConnectionError, OSError):
        pass


async def _write_redirect(writer: asyncio.StreamWriter, location: str) -> None:
    body = b""
    head = (
        "HTTP/1.1 302 Found\r\n"
        f"Location: {location}\r\n"
        "Content-Length: 0\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    try:
        writer.write(head + body)
        await writer.drain()
    except (ConnectionError, OSError):
        pass


async def _close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
        await writer.wait_closed()
    except (OSError, asyncio.CancelledError):
        pass


def _peer(writer: asyncio.StreamWriter) -> str:
    try:
        peer = writer.get_extra_info("peername")
        return f"{peer[0]}:{peer[1]}" if peer else "?"
    except Exception:
        return "?"


def _peer_certificate(writer: asyncio.StreamWriter) -> bytes | None:
    """Return the peer's DER-encoded certificate.

    ``StreamWriter.get_extra_info("cert")`` returns ``None``; the SSL
    object lives on the transport and exposes the certificate through
    ``getpeercert(binary_form=True)``.
    """
    transport = writer.transport
    ssl_object = transport.get_extra_info("ssl_object")
    if ssl_object is not None:
        try:
            return ssl_object.getpeercert(binary_form=True)
        except (ssl.SSLError, ValueError):
            return None
    try:
        return writer.get_extra_info("cert", None)
    except Exception:  # pragma: no cover - defensive
        return None


__all__ = ["TcpProxyService", "ProxyStats", "_parse_request_head"]
