"""Tests for the international -> domestic (.com -> .cn) redirect.

This is the core behaviour of the accelerator: a client asking for
``assets1.xboxlive.com`` must be sent to ``assets2.xboxlive.cn`` so the
download is served by domestic CDN nodes.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core import host_rules
from app.core.config import get_settings
from app.core.engine import AccelerationEngine
from app.core.host_mappings import HostMappingStore


@pytest.fixture
def engine(tmp_path):
    settings = get_settings()
    settings.reset()
    mappings = HostMappingStore(tmp_path / "hosts.json")
    eng = AccelerationEngine(settings, mappings)
    eng.refresh()
    return eng


# ---------------------------------------------------------------------------
# redirect table
# ---------------------------------------------------------------------------
def test_assets1_com_redirects_to_cn():
    """The headline behaviour: assets1.xboxlive.com -> assets2.xboxlive.cn."""
    assert (
        host_rules.cn_redirect_target("assets1.xboxlive.com")
        == "assets2.xboxlive.cn"
    )


@pytest.mark.parametrize(
    "host,expected",
    [
        ("assets1.xboxlive.com", "assets2.xboxlive.cn"),
        ("assets2.xboxlive.com", "assets2.xboxlive.cn"),
        ("xvcf1.xboxlive.com", "assets2.xboxlive.cn"),
        ("d1.xboxlive.com", "assets2.xboxlive.cn"),
        ("dlassets.xboxlive.com", "dlassets2.xboxlive.cn"),
        ("dlassets2.xboxlive.com", "dlassets2.xboxlive.cn"),
        ("assets1.xboxlive.cn", "assets2.xboxlive.cn"),
        ("dlassets.xboxlive.cn", "dlassets2.xboxlive.cn"),
    ],
)
def test_redirect_table(host, expected):
    assert host_rules.cn_redirect_target(host) == expected


def test_redirect_is_case_insensitive():
    assert (
        host_rules.cn_redirect_target("ASSETS1.XBOXLIVE.COM")
        == "assets2.xboxlive.cn"
    )


def test_unknown_host_has_no_redirect():
    assert host_rules.cn_redirect_target("example.com") is None


# ---------------------------------------------------------------------------
# group mapping
# ---------------------------------------------------------------------------
def test_cn_group_for_international_host():
    """assets1.xboxlive.com is served by the CN1 group."""
    assert host_rules.cn_group_for_host("assets1.xboxlive.com") == "xbox_cn1"


def test_cn_group_for_dlassets_is_cn2():
    assert host_rules.cn_group_for_host("dlassets.xboxlive.com") == "xbox_cn2"


def test_cn_group_for_domestic_host():
    assert host_rules.cn_group_for_host("assets2.xboxlive.cn") == "xbox_cn1"
    assert host_rules.cn_group_for_host("dlassets2.xboxlive.cn") == "xbox_cn2"


def test_cn_group_for_unrelated_host():
    assert host_rules.cn_group_for_host("example.com") is None


# ---------------------------------------------------------------------------
# engine: the DNS answer for a .com host is the domestic address
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_engine_answers_com_host_with_cn1_address(engine):
    """With a CN1 acceleration address, a .com host resolves to it."""
    engine.settings.update({"xbox_cn1_ip": "60.167.226.211"})
    engine.refresh()

    result = await engine.resolve("assets1.xboxlive.com")
    assert result.ips == ["60.167.226.211"]
    assert result.group == "xbox_cn1"


@pytest.mark.asyncio
async def test_engine_points_com_host_at_the_local_proxy(engine):
    """With a proxy address configured, the .com host resolves to the proxy.

    That is what makes the client receive the 302 redirect to the
    domestic host — the same mechanism as the C# original, which maps
    the ".com" hosts to the local IP.
    """
    engine.settings.update(
        {"xbox_cn1_ip": "60.167.226.211", "local_proxy_ip": "192.168.1.10"}
    )
    engine.refresh()

    result = await engine.resolve("assets1.xboxlive.com")
    assert result.ips == ["192.168.1.10"]
    assert result.source == "redirect"
    assert result.group == "xbox_cn1"


@pytest.mark.asyncio
async def test_engine_answers_dlassets_com_with_cn2_address(engine):
    engine.settings.update({"xbox_cn2_ip": "182.61.128.209"})
    engine.refresh()

    result = await engine.resolve("dlassets.xboxlive.com")
    assert result.ips == ["182.61.128.209"]
    assert result.group == "xbox_cn2"


@pytest.mark.asyncio
async def test_detected_proxy_ip_survives_a_settings_refresh(engine):
    """Saving an acceleration address must not disable the redirect.

    Changing a setting calls engine.refresh(), which re-reads the
    settings. The auto-detected proxy address has to survive that,
    otherwise the ".com" hosts stop being redirected.
    """
    engine.set_detected_proxy_ip("192.168.1.10")
    engine.refresh()
    assert engine.local_proxy_ip == "192.168.1.10"

    # A settings change (e.g. saving an acceleration address) triggers a
    # refresh — the proxy address must still be in place.
    engine.settings.update({"xbox_cn1_ip": "60.167.226.211"})
    engine.refresh()
    assert engine.local_proxy_ip == "192.168.1.10"

    result = await engine.resolve("assets1.xboxlive.com")
    assert result.ips == ["192.168.1.10"]
    assert result.source == "redirect"


@pytest.mark.asyncio
async def test_explicit_proxy_ip_overrides_the_detected_one(engine):
    engine.set_detected_proxy_ip("192.168.1.10")
    engine.settings.update({"local_proxy_ip": "10.0.0.1"})
    engine.refresh()
    assert engine.local_proxy_ip == "10.0.0.1"


@pytest.mark.asyncio
async def test_cn_fallback_wins_over_global_address(engine):
    """The international host must NOT be served by the overseas global address.

    The ".com" hosts also belong to the XboxGlobal rule, but using the
    global (overseas) address would defeat the accelerator — the whole
    point is to serve the download from a domestic node.
    """
    engine.settings.update(
        {"xbox_global_ip": "23.64.58.45", "xbox_cn1_ip": "60.167.226.211"}
    )
    engine.refresh()

    result = await engine.resolve("assets1.xboxlive.com")
    assert result.ips == ["60.167.226.211"]
    assert result.group == "xbox_cn1"


@pytest.mark.asyncio
async def test_manual_mapping_wins_over_cn_fallback(engine):
    engine.settings.update({"xbox_cn1_ip": "60.167.226.211"})
    engine.host_mappings.add("assets1.xboxlive.com", "9.9.9.9")
    engine.refresh()

    result = await engine.resolve("assets1.xboxlive.com")
    assert result.source == "manual"
    assert result.ips == ["9.9.9.9"]


# ---------------------------------------------------------------------------
# proxy: the redirect is issued
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_proxy_redirects_com_to_cn(engine):
    """The HTTP listener answers assets1.xboxlive.com with a 302 to the .cn host."""
    from app.services.tcp_proxy import TcpProxyService

    proxy = TcpProxyService(engine, "127.0.0.1", http_port=0, https_port=0)
    await proxy.start()
    try:
        port = proxy._http_server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /x HTTP/1.1\r\nHost: assets1.xboxlive.com\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.read(4096), timeout=10)
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.stop()

    assert b"302 Found" in response
    assert b"assets2.xboxlive.cn" in response


@pytest.mark.asyncio
async def test_proxy_redirects_dlassets_to_cn2(engine):
    from app.services.tcp_proxy import TcpProxyService

    proxy = TcpProxyService(engine, "127.0.0.1", http_port=0, https_port=0)
    await proxy.start()
    try:
        port = proxy._http_server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /x HTTP/1.1\r\nHost: dlassets.xboxlive.com\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.read(4096), timeout=10)
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.stop()

    assert b"302 Found" in response
    assert b"dlassets2.xboxlive.cn" in response


# ---------------------------------------------------------------------------
# the "显示 Xbox 游戏下载链接" switch
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "host,on_target,off_target",
    [
        ("assets1.xboxlive.com", "assets2.xboxlive.cn", "assets1.xboxlive.cn"),
        ("xvcf1.xboxlive.com", "assets2.xboxlive.cn", "assets1.xboxlive.cn"),
        ("d1.xboxlive.com", "assets2.xboxlive.cn", "d1.xboxlive.cn"),
        ("dlassets.xboxlive.com", "dlassets2.xboxlive.cn", "dlassets.xboxlive.cn"),
    ],
)
def test_switch_changes_the_redirect_target(host, on_target, off_target):
    assert host_rules.cn_redirect_target(host, True) == on_target
    assert host_rules.cn_redirect_target(host, False) == off_target


@pytest.mark.parametrize(
    "host,on_target,off_target",
    [
        # The "2" endpoints always redirect to their "2" counterpart.
        ("assets2.xboxlive.com", "assets2.xboxlive.cn", "assets2.xboxlive.cn"),
        ("d2.xboxlive.com", "assets2.xboxlive.cn", "assets2.xboxlive.cn"),
        ("dlassets2.xboxlive.com", "dlassets2.xboxlive.cn", "dlassets2.xboxlive.cn"),
    ],
)
def test_switch_does_not_affect_the_2_endpoints(host, on_target, off_target):
    assert host_rules.cn_redirect_target(host, True) == on_target
    assert host_rules.cn_redirect_target(host, False) == off_target


@pytest.mark.parametrize(
    "host",
    ["assets1.xboxlive.cn", "d1.xboxlive.cn", "dlassets.xboxlive.cn"],
)
def test_domestic_1_hosts_are_served_directly_when_the_switch_is_off(host):
    """With the switch off, a domestic "1" host needs no redirect.

    Redirecting it to itself would loop forever, so the proxy must serve
    it from the acceleration address directly.
    """
    assert host_rules.cn_redirect_target(host, False) is None
    assert host_rules.cn_redirect_target(host, True) is not None


def test_no_host_is_ever_redirected_to_itself():
    """A self-redirect would make the client loop forever."""
    for table in (
        host_rules.XBOX_CN_REDIRECTS,
        host_rules.XBOX_CN_REDIRECTS_WITHOUT_GAME_LINKS,
    ):
        for source, target in table.items():
            assert source != target, f"{source} redirects to itself"


@pytest.mark.asyncio
async def test_domestic_1_host_is_relayed_when_the_switch_is_off(engine):
    """With the switch off, assets1.xboxlive.cn is relayed, not redirected."""
    from app.services.tcp_proxy import TcpProxyService

    engine.settings.update({"xbox_cn1_ip": "60.167.226.211"})
    engine.refresh()
    engine.game_links_shown = False

    proxy = TcpProxyService(engine, "127.0.0.1", http_port=0, https_port=0)
    await proxy.start()
    try:
        port = proxy._http_server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /x HTTP/1.1\r\nHost: assets1.xboxlive.cn\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.read(4096), timeout=10)
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.stop()

    # Not a redirect — the request is relayed to the acceleration address.
    assert b"302 Found" not in response


def test_switch_default_is_on():
    """The switch defaults to enabled, matching the C# original."""
    from app.core.config import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["is_xbox_game_download_links_shown"] is True


@pytest.mark.asyncio
async def test_engine_uses_the_switch(engine):
    """Turning the switch off changes the DNS answer for the "1" hosts."""
    engine.settings.update({"xbox_cn1_ip": "60.167.226.211"})
    engine.refresh()

    engine.game_links_shown = False
    result = await engine.resolve("assets1.xboxlive.com")
    # The "1" host keeps its own number, but is still served by CN1.
    assert result.rule_key == "assets1.xboxlive.cn"
    assert result.group == "xbox_cn1"

    engine.game_links_shown = True
    result = await engine.resolve("assets1.xboxlive.com")
    assert result.rule_key == "assets2.xboxlive.cn"


@pytest.mark.asyncio
async def test_proxy_uses_the_switch(engine):
    """The HTTP listener honours the switch."""
    from app.services.tcp_proxy import TcpProxyService

    engine.game_links_shown = False
    proxy = TcpProxyService(engine, "127.0.0.1", http_port=0, https_port=0)
    await proxy.start()
    try:
        port = proxy._http_server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /x HTTP/1.1\r\nHost: assets1.xboxlive.com\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.read(4096), timeout=10)
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.stop()

    assert b"302 Found" in response
    assert b"assets1.xboxlive.cn" in response
    assert b"assets2.xboxlive.cn" not in response
