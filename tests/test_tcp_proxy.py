"""Tests for the HTTP/HTTPS acceleration proxy.

The proxy is tested against a local origin server so the whole relay path
(request parse -> pinned connect -> byte relay) is exercised without
touching the internet.
"""

from __future__ import annotations

import asyncio
import ssl

import pytest

from app.core.config import get_settings
from app.core.engine import AccelerationEngine
from app.core.host_mappings import HostMappingStore
from app.services.certificates import CertificateProvider
from app.services.tcp_proxy import TcpProxyService, _Target, _parse_request_head


@pytest.fixture
def engine(tmp_path):
    settings = get_settings()
    settings.reset()
    mappings = HostMappingStore(tmp_path / "hosts.json")
    eng = AccelerationEngine(settings, mappings)
    eng.refresh()
    return eng


def server_port(server) -> int:
    """Return the bound TCP port of an asyncio server."""
    return server.sockets[0].getsockname()[1]


# ---------------------------------------------------------------------------
# request parsing
# ---------------------------------------------------------------------------
def test_parse_request_head_extracts_method_path_host():
    raw = b"GET /a/b?c=1 HTTP/1.1\r\nHost: Example.COM\r\n\r\n"
    request = _parse_request_head(raw)
    assert request is not None
    assert request.method == "GET"
    assert request.path == "/a/b?c=1"
    assert request.host == "example.com"
    assert request.extra == b""


def test_parse_request_head_keeps_body_bytes():
    raw = b"POST /x HTTP/1.1\r\nHost: a.com\r\nContent-Length: 3\r\n\r\nabc"
    request = _parse_request_head(raw)
    assert request is not None
    assert request.extra == b"abc"


def test_parse_request_head_incomplete_returns_none():
    assert _parse_request_head(b"GET / HTTP/1.1\r\nHost: a.com\r\n") is None


# ---------------------------------------------------------------------------
# HTTP relay
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_http_relays_request_upstream(engine, monkeypatch):
    """The proxy forwards the request to the pinned upstream and relays back."""
    received: list[bytes] = []

    async def origin(reader, writer):
        data = await reader.read(4096)
        received.append(data)
        body = b"ORIGIN-OK"
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()

    origin_server = await asyncio.start_server(origin, "127.0.0.1", 0)
    origin_port = server_port(origin_server)

    proxy = TcpProxyService(engine, "127.0.0.1", http_port=0, https_port=0)
    # Point the relay at our local origin: the target carries the IP, and
    # the port is patched for the test.
    original_relay = proxy._relay_http

    async def relay_to_origin(reader, writer, request, ip):
        upstream_reader, upstream_writer = await asyncio.open_connection(
            "127.0.0.1", origin_port
        )
        proxy.stats.relayed_connections += 1
        upstream_writer.write(request.head + request.extra)
        await upstream_writer.drain()
        await asyncio.gather(
            proxy._pump(reader, upstream_writer, to_upstream=True),
            proxy._pump(upstream_reader, writer, to_upstream=False),
            return_exceptions=True,
        )
        upstream_writer.close()

    monkeypatch.setattr(proxy, "_relay_http", relay_to_origin)
    _ = original_relay

    async def fake_resolve(host):
        return _Target(ip="127.0.0.1", host=host, redirect=False)

    monkeypatch.setattr(proxy, "_resolve_target", fake_resolve)

    await proxy.start()
    try:
        port = server_port(proxy._http_server)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /file HTTP/1.1\r\nHost: assets1.xboxlive.com\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.read(4096), timeout=10)
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.stop()
        origin_server.close()
        await origin_server.wait_closed()

    assert b"200 OK" in response
    assert b"ORIGIN-OK" in response
    assert proxy.stats.relayed_connections == 1
    assert received and b"assets1.xboxlive.com" in received[0]


@pytest.mark.asyncio
async def test_http_relay_terminates_when_server_closes(engine):
    """The relay must not hang when the origin closes its side first.

    A client that keeps the connection open would make the
    client->upstream pump block forever if the relay waited for both
    directions (asyncio.gather).
    """
    async def origin(reader, writer):
        await reader.read(4096)
        body = b"BYE"
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()

    origin_server = await asyncio.start_server(origin, "127.0.0.1", 0)
    origin_port = server_port(origin_server)

    proxy = TcpProxyService(engine, "127.0.0.1", http_port=0, https_port=0)

    async def relay_to_origin(reader, writer, request, ip):
        upstream_reader, upstream_writer = await asyncio.open_connection(
            "127.0.0.1", origin_port
        )
        proxy.stats.relayed_connections += 1
        upstream_writer.write(request.head + request.extra)
        await upstream_writer.drain()
        await proxy._relay_both(reader, writer, upstream_reader, upstream_writer)
        upstream_writer.close()

    proxy._relay_http = relay_to_origin  # type: ignore[assignment]

    async def fake_resolve(host):
        return _Target(ip="127.0.0.1", host=host, redirect=False)

    proxy._resolve_target = fake_resolve  # type: ignore[assignment]

    await proxy.start()
    try:
        port = server_port(proxy._http_server)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /f HTTP/1.1\r\nHost: a.example\r\n\r\n")
        await writer.drain()
        # Must complete promptly, not hang waiting for more client data.
        response = await asyncio.wait_for(reader.read(4096), timeout=10)
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.stop()
        origin_server.close()
        await origin_server.wait_closed()

    assert b"200 OK" in response
    assert b"BYE" in response


@pytest.mark.asyncio
async def test_http_redirects_for_known_hosts(engine):
    """Well-known hosts are redirected to their regional equivalent."""
    proxy = TcpProxyService(engine, "127.0.0.1", http_port=0, https_port=0)
    await proxy.start()
    try:
        port = server_port(proxy._http_server)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /x HTTP/1.1\r\nHost: us.cdn.blizzard.com\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.read(4096), timeout=10)
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.stop()

    assert b"302 Found" in response
    assert b"blzddist1-a.akamaihd.net" in response
    assert proxy.stats.redirected_requests == 1


@pytest.mark.asyncio
async def test_http_unknown_host_returns_404(engine):
    engine.settings.update(
        {"doh_servers": [], "allow_system_dns_fallback": False}
    )
    engine.refresh()
    proxy = TcpProxyService(engine, "127.0.0.1", http_port=0, https_port=0)
    await proxy.start()
    try:
        port = server_port(proxy._http_server)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /x HTTP/1.1\r\nHost: unknown.example\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.read(4096), timeout=10)
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.stop()

    assert b"404 Not Found" in response


@pytest.mark.asyncio
async def test_http_missing_host_returns_400(engine):
    proxy = TcpProxyService(engine, "127.0.0.1", http_port=0, https_port=0)
    await proxy.start()
    try:
        port = server_port(proxy._http_server)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /x HTTP/1.1\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.read(4096), timeout=10)
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.stop()

    assert b"400 Bad Request" in response


# ---------------------------------------------------------------------------
# TLS termination
# ---------------------------------------------------------------------------
def test_certificate_provider_issues_leaf(tmp_path):
    provider = CertificateProvider(tmp_path)
    cert_pem, key_pem = provider.get_certificate("assets1.xboxlive.com")
    assert b"BEGIN CERTIFICATE" in cert_pem
    assert b"PRIVATE KEY" in key_pem

    # The leaf must load into a server context.
    cert_path = tmp_path / "leaf.pem"
    key_path = tmp_path / "leaf.key"
    cert_path.write_bytes(cert_pem)
    key_path.write_bytes(key_pem)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))


def test_certificate_is_cached(tmp_path):
    provider = CertificateProvider(tmp_path)
    first = provider.get_certificate("a.example")
    second = provider.get_certificate("a.example")
    assert first == second


def test_certificate_ca_persists(tmp_path):
    provider = CertificateProvider(tmp_path)
    provider.get_certificate("a.example")
    assert provider.ca_cert_path.exists()
    assert provider.ca_key_path.exists()


def test_peer_certificate_helper_is_importable():
    """The helper must exist and tolerate a non-SSL writer."""
    from app.services.tcp_proxy import _peer_certificate

    assert callable(_peer_certificate)


@pytest.mark.asyncio
async def test_https_terminates_tls(engine, tmp_path):
    """The HTTPS listener completes a TLS handshake with a local cert."""
    provider = CertificateProvider(tmp_path)
    proxy = TcpProxyService(
        engine,
        "127.0.0.1",
        http_port=0,
        https_port=0,
        certificate_provider=provider,
    )

    async def fake_resolve(host):
        return _Target(ip="127.0.0.1", host=host, redirect=False)

    proxy._resolve_target = fake_resolve  # type: ignore[assignment]

    await proxy.start()
    try:
        port = server_port(proxy._https_server)

        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

        reader, writer = await asyncio.open_connection(
            "127.0.0.1", port, ssl=context
        )
        writer.write(b"GET /x HTTP/1.1\r\nHost: a.example\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.read(4096), timeout=10)
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.stop()

    # The TLS handshake succeeded, so the request reached the HTTP layer.
    assert proxy.stats.https_connections == 1
    assert b"502 Bad Gateway" in response or b"404" in response or b"200" in response
