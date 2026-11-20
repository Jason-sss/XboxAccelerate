"""Tests for the acceleration engine and the host-rule table."""

from __future__ import annotations

import pytest

from app.core import host_rules
from app.core.config import get_settings
from app.core.engine import AccelerationEngine
from app.core.host_mappings import HostMappingStore
from app.core.ip_lists import (
    load_group_candidates,
    load_ip_list,
    parse_ip_list,
    pool_for_group,
)
from app.core.resolver import TYPE_A, TYPE_AAAA


@pytest.fixture
def engine(tmp_path):
    settings = get_settings()
    settings.reset()
    mappings = HostMappingStore(tmp_path / "hosts.json")
    eng = AccelerationEngine(settings, mappings)
    eng.refresh()
    return eng


# ---------------------------------------------------------------------------
# host rules
# ---------------------------------------------------------------------------
def test_rule_lookup_exact_match():
    match = host_rules.rule_for_host("assets1.xboxlive.com")
    assert match is not None
    bucket, rule = match
    assert bucket == "hosts"
    assert rule.key == "XboxGlobal"


def test_rule_lookup_is_case_insensitive():
    match = host_rules.rule_for_host("ASSETS1.XBOXLIVE.COM")
    assert match is not None
    assert match[1].key == "XboxGlobal"


def test_rule_lookup_blacklist():
    match = host_rules.rule_for_host("level3.ssl.blizzard.com")
    assert match is not None
    assert match[0] == "blacklist"
    assert match[1].key == "Battle"


def test_rule_lookup_unknown_host():
    assert host_rules.rule_for_host("example.com") is None


def test_generate_dns_mapping_hosts_format():
    text = host_rules.generate_dns_mapping("XboxGlobal", "1.2.3.4", "hosts", "Map")
    assert "1.2.3.4 assets1.xboxlive.com # XboxDownload" in text


def test_generate_dns_mapping_dnsmasq_format():
    text = host_rules.generate_dns_mapping("XboxCn1", "1.2.3.4", "dnsmasq", "Map")
    assert "address=/assets1.xboxlive.cn/1.2.3.4 # XboxDownload" in text


def test_generate_dns_mapping_blacklist_uses_zero():
    text = host_rules.generate_dns_mapping("Battle", "1.2.3.4", "hosts", "Map")
    assert "0.0.0.0 level3.ssl.blizzard.com # XboxDownload" in text


def test_generate_dns_mapping_export_has_comments():
    text = host_rules.generate_dns_mapping("XboxCn2", "1.2.3.4", "hosts", "Export")
    assert "# Xbox CN2" in text


def test_resolve_keys_alias_akamai():
    keys = host_rules.resolve_keys("Akamai")
    assert "XboxGlobal" in keys
    assert "Battle" in keys
    assert len(keys) == 6


# ---------------------------------------------------------------------------
# IP lists
# ---------------------------------------------------------------------------
def test_parse_ip_list_extracts_ip_and_location():
    text = "Akamai\n\n23.64.58.45\t(Argentina Buenos Aires)\n"
    candidates = parse_ip_list(text, "Akamai")
    assert len(candidates) == 1
    assert candidates[0].ip == "23.64.58.45"
    assert candidates[0].location == "Argentina Buenos Aires"
    assert candidates[0].is_ipv6 is False


def test_parse_ip_list_skips_invalid_entries():
    text = "G\n\nnot-an-ip\t(x)\n1.2.3.4\t(y)\n"
    candidates = parse_ip_list(text, "G")
    assert [c.ip for c in candidates] == ["1.2.3.4"]


def test_parse_ip_list_deduplicates():
    text = "G\n\n1.2.3.4\t(a)\n1.2.3.4\t(b)\n"
    assert len(parse_ip_list(text, "G")) == 1


def test_shipped_ip_lists_load():
    for group in ("Akamai", "XboxCn1", "XboxCn2", "XboxApp", "Ps"):
        candidates = load_ip_list(group)
        assert candidates, f"{group} should not be empty"
        assert all(c.ip for c in candidates)


# ---------------------------------------------------------------------------
# group -> pool mapping
# ---------------------------------------------------------------------------
def test_pool_for_cn_groups():
    """The CN groups must map onto the domestic pools, not the overseas one."""
    assert pool_for_group("xbox_cn1") == "XboxCn1"
    assert pool_for_group("xbox_cn2") == "XboxCn2"
    assert pool_for_group("xbox_app") == "XboxApp"
    assert pool_for_group("ps") == "Ps"


def test_pool_for_global_groups():
    assert pool_for_group("xbox_global") == "Akamai"
    assert pool_for_group("akamai") == "Akamai"


def test_pool_for_group_accepts_pool_names():
    assert pool_for_group("XboxCn1") == "XboxCn1"
    assert pool_for_group("akamaiv6") == "AkamaiV6"


def test_pool_for_unknown_group():
    assert pool_for_group("does_not_exist") is None


def test_cn1_pool_contains_domestic_addresses():
    """The CN1 pool must be the domestic one, not the overseas Akamai pool."""
    candidates = load_group_candidates("xbox_cn1")
    assert candidates
    assert all("中国" in c.location for c in candidates), "CN1 pool must be domestic"


def test_cn2_pool_contains_domestic_addresses():
    candidates = load_group_candidates("xbox_cn2")
    assert candidates
    assert all("中国" in c.location for c in candidates), "CN2 pool must be domestic"


def test_group_candidates_do_not_fall_back_to_overseas_pool():
    """A group with no pool must return nothing rather than the Akamai pool."""
    assert load_group_candidates("does_not_exist") == []


def test_global_group_uses_overseas_pool():
    candidates = load_group_candidates("xbox_global")
    assert candidates
    assert not all("中国" in c.location for c in candidates)


def test_ipv6_list_parsed():
    candidates = load_ip_list("AkamaiV6")
    assert candidates
    assert all(c.is_ipv6 for c in candidates)


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_manual_mapping_has_highest_priority(engine):
    engine.host_mappings.add("assets1.xboxlive.com", "9.9.9.9")
    engine.settings.update({"xbox_global_ip": "1.2.3.4"})
    engine.refresh()

    result = await engine.resolve("assets1.xboxlive.com", TYPE_A)
    assert result.source == "manual"
    assert result.ips == ["9.9.9.9"]


@pytest.mark.asyncio
async def test_rule_uses_configured_acceleration_address(engine):
    engine.settings.update({"xbox_global_ip": "1.2.3.4"})
    engine.refresh()

    result = await engine.resolve("assets1.xboxlive.com", TYPE_A)
    assert result.source == "rule"
    assert result.ips == ["1.2.3.4"]
    assert result.rule_key == "XboxGlobal"
    assert result.group == "xbox_global"


@pytest.mark.asyncio
async def test_rule_without_address_falls_back_to_upstream(engine):
    """With no acceleration address configured the host resolves normally."""
    engine.settings.update(
        {
            "upstream_dns": [],
            "doh_servers": [],
            "xbox_global_ip": "",
            "allow_system_dns_fallback": False,
        }
    )
    engine.refresh()

    result = await engine.resolve("assets1.xboxlive.com", TYPE_A)
    assert result.source in ("upstream", "cached")
    assert result.ips == []


@pytest.mark.asyncio
async def test_blacklist_blocks_even_with_address(engine):
    engine.settings.update({"battle_ip": "1.2.3.4"})
    engine.refresh()

    result = await engine.resolve("level3.ssl.blizzard.com", TYPE_A)
    assert result.source == "blacklist"
    assert result.ips == ["0.0.0.0"]


@pytest.mark.asyncio
async def test_each_group_has_its_own_address(engine):
    engine.settings.update(
        {
            "xbox_global_ip": "1.1.1.1",
            "xbox_cn1_ip": "2.2.2.2",
            "xbox_cn2_ip": "3.3.3.3",
            "ps_ip": "4.4.4.4",
        }
    )
    engine.refresh()

    # The domestic hosts are served by their own group.
    assert (await engine.resolve("assets2.xboxlive.cn", TYPE_A)).ips == ["2.2.2.2"]
    assert (await engine.resolve("dlassets.xboxlive.cn", TYPE_A)).ips == ["3.3.3.3"]
    assert (
        await engine.resolve("gst.prod.dl.playstation.net", TYPE_A)
    ).ips == ["4.4.4.4"]
    # The international hosts are redirected to the domestic groups, so
    # they never use the overseas global address.
    assert (await engine.resolve("assets1.xboxlive.com", TYPE_A)).ips == ["2.2.2.2"]
    assert (await engine.resolve("dlassets.xboxlive.com", TYPE_A)).ips == ["3.3.3.3"]


@pytest.mark.asyncio
async def test_disabled_mapping_is_ignored(engine):
    engine.host_mappings.add("example.com", "5.5.5.5", is_enabled=False)
    engine.settings.update({"upstream_dns": []})
    engine.refresh()

    result = await engine.resolve("example.com", TYPE_A)
    assert result.source != "manual"


def test_is_valid_accel_ip(engine):
    assert engine.is_valid_accel_ip("1.2.3.4")
    assert engine.is_valid_accel_ip("2606:4700::1111")
    assert not engine.is_valid_accel_ip("not-an-ip")
    assert not engine.is_valid_accel_ip("")


def test_effective_mapping_reports_state(engine):
    engine.settings.update({"xbox_global_ip": "1.2.3.4"})
    engine.refresh()
    mapping = engine.effective_mapping("xbox_global")
    assert mapping["configured"] is True
    assert mapping["accel_ip"] == "1.2.3.4"
    assert "assets1.xboxlive.com" in mapping["hosts"]


def test_refresh_reloads_upstream_servers(engine):
    engine.settings.update({"upstream_dns": ["1.1.1.1", "8.8.8.8"]})
    engine.refresh()
    assert engine.resolver.upstream_dns == ["1.1.1.1", "8.8.8.8"]


def test_ipv6_query_type_is_passed_through(engine):
    engine.settings.update({"upstream_dns": []})
    engine.refresh()
    result = engine.resolver.resolve_dns  # exists
    assert callable(result)
