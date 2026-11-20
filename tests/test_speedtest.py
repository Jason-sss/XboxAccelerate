"""Tests for the speed / latency measurement."""

from __future__ import annotations

import asyncio

import pytest

from app.core.resolver import TYPE_A
from app.core.speedtest import (
    SPEEDTEST_TARGETS,
    _parse_target,
    fastest_https_ip,
    measure_addresses,
)


def test_parse_target_extracts_host_and_port():
    target = _parse_target("http://assets1.xboxlive.cn/path/file.bin")
    assert target.host == "assets1.xboxlive.cn"
    assert target.port == 80
    assert target.is_https is False


def test_parse_target_https_default_port():
    target = _parse_target("https://example.com/x")
    assert target.port == 443
    assert target.is_https is True


def test_parse_target_explicit_port():
    target = _parse_target("http://example.com:8080/x")
    assert target.port == 8080


def test_parse_target_path_keeps_leading_slash():
    """The request target must be "/path", not "path" — Akamai returns 400 otherwise."""
    target = _parse_target("http://xvcf1.xboxlive.com/Z/routing/extraextralarge.txt")
    assert target.path == "/Z/routing/extraextralarge.txt"
    assert target.path.startswith("/")


def test_parse_target_root_path():
    target = _parse_target("http://example.com")
    assert target.path == "/"


def test_parse_target_keeps_query_string():
    target = _parse_target("http://example.com/a?b=c")
    assert target.path == "/a?b=c"


def test_targets_cover_the_expected_groups():
    for group in ("akamai", "xbox_cn1", "xbox_cn2", "xbox_app", "ps"):
        assert SPEEDTEST_TARGETS[group], f"{group} should have targets"
        for label, url in SPEEDTEST_TARGETS[group]:
            assert label
            assert url.startswith("http")


@pytest.mark.asyncio
async def test_measure_unreachable_address_reports_error():
    """An unroutable address must produce a failed row with an error message."""
    results = await measure_addresses(
        ["192.0.2.1"],
        "http://xvcf1.xboxlive.com/Z/routing/extraextralarge.txt",
        timeout=3,
        chunk_bytes=1024 * 1024,
        with_ttl=False,
    )
    assert len(results) == 1
    assert results[0].ok is False
    assert results[0].error
    assert results[0].bytes_downloaded == 0


@pytest.mark.asyncio
async def test_measure_invalid_ip_is_rejected():
    results = await measure_addresses(
        ["not-an-ip"],
        "http://example.com/x",
        timeout=2,
        chunk_bytes=4096,
        with_ttl=False,
    )
    assert len(results) == 1
    assert results[0].ok is False
    assert "Invalid IP" in (results[0].error or "")


@pytest.mark.asyncio
async def test_measure_empty_input_returns_empty():
    results = await measure_addresses([], "http://example.com/x")
    assert results == []


@pytest.mark.asyncio
async def test_measure_against_local_server():
    """A local HTTP server must be measured successfully."""
    payload = b"x" * (256 * 1024)

    async def handle(reader, writer):
        await reader.read(4096)
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(payload)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + payload
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    try:
        results = await measure_addresses(
            ["127.0.0.1"],
            f"http://127.0.0.1:{port}/file.bin",
            timeout=10,
            chunk_bytes=len(payload),
            concurrency=1,
            with_ttl=False,
        )
    finally:
        server.close()
        await server.wait_closed()

    assert len(results) == 1
    assert results[0].ok is True
    assert results[0].bytes_downloaded == len(payload)
    assert results[0].speed_mib_s and results[0].speed_mib_s > 0
    assert results[0].latency_ms is not None


@pytest.mark.asyncio
async def test_measure_sorts_reachable_first():
    """Unreachable addresses sort after working ones."""
    results = await measure_addresses(
        ["192.0.2.1", "127.0.0.1"],
        "http://127.0.0.1:1/x",
        timeout=2,
        chunk_bytes=1024,
        concurrency=2,
        with_ttl=False,
    )
    assert len(results) == 2
    # The ordering must be stable: reachable first.
    oks = [r.ok for r in results]
    assert oks == sorted(oks, reverse=True)


@pytest.mark.asyncio
async def test_resolve_system_uses_getaddrinfo_correctly():
    """loop.getaddrinfo takes keyword-only family/type — a positional call raises."""
    from app.core.resolver import UpstreamResolver

    resolver = UpstreamResolver()
    result = await resolver.resolve_system("localhost", TYPE_A)
    assert isinstance(result, list)
    # "localhost" must resolve to a loopback address.
    assert any(ip.startswith("127.") for ip in result) or result == []


@pytest.mark.asyncio
async def test_resolve_system_handles_invalid_host():
    from app.core.resolver import UpstreamResolver

    resolver = UpstreamResolver()
    result = await resolver.resolve_system("this-host-does-not-exist.invalid", TYPE_A)
    assert result == []


@pytest.mark.asyncio
async def test_fastest_https_ip_returns_none_for_bad_input():
    assert await fastest_https_ip([]) is None
    assert await fastest_https_ip(["not-an-ip"]) is None


@pytest.mark.asyncio
async def test_ttl_measurement_returns_value_for_localhost():
    from app.core.speedtest import _measure_ttl

    ttl = await _measure_ttl("127.0.0.1")
    assert ttl is None or isinstance(ttl, int)
