"""Tests for the HTTP API."""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A TestClient bound to an isolated state directory."""
    monkeypatch.setenv("XBD_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("XBD_DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))

    # Re-import the app so the settings store picks up the new paths.
    import importlib

    from app.core import config as config_mod

    importlib.reload(config_mod)
    for module in ("app.core.host_mappings", "app.core.engine", "app.core.ip_lists"):
        importlib.reload(importlib.import_module(module))
    importlib.reload(importlib.import_module("app.main"))

    from app.main import create_app

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# health / status
# ---------------------------------------------------------------------------
def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_status_shape(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert "settings" in body
    assert "accel_addresses" in body
    assert "dns" in body
    assert "proxy" in body


def test_root_serves_the_ui(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "XboxDownload" in r.text


def test_static_assets_are_served(client):
    assert client.get("/style.css").status_code == 200
    assert client.get("/app.js").status_code == 200


# ---------------------------------------------------------------------------
# acceleration addresses
# ---------------------------------------------------------------------------
def test_list_accel_groups(client):
    r = client.get("/api/accel")
    assert r.status_code == 200
    groups = r.json()["groups"]
    assert len(groups) == 8
    assert all(g["accel_ip"] == "" for g in groups)


def test_set_accel_address(client):
    r = client.put(
        "/api/accel", json={"group": "xbox_global", "ip": "23.64.58.45"}
    )
    assert r.status_code == 200
    assert r.json()["accel_ip"] == "23.64.58.45"
    assert r.json()["configured"] is True

    # Persisted?
    r = client.get("/api/accel")
    entry = next(g for g in r.json()["groups"] if g["group"] == "xbox_global")
    assert entry["accel_ip"] == "23.64.58.45"


def test_set_accel_rejects_invalid_ip(client):
    r = client.put("/api/accel", json={"group": "xbox_global", "ip": "999.1.1.1"})
    assert r.status_code == 422


def test_set_accel_rejects_unknown_group(client):
    r = client.put("/api/accel", json={"group": "nope", "ip": "1.2.3.4"})
    assert r.status_code == 422


def test_clear_accel(client):
    client.put("/api/accel", json={"group": "xbox_global", "ip": "1.2.3.4"})
    r = client.post("/api/accel/clear")
    assert r.status_code == 200
    r = client.get("/api/accel")
    assert all(g["accel_ip"] == "" for g in r.json()["groups"])


def test_accel_address_affects_resolution(client):
    """A domestic host resolves to the acceleration address of its group."""
    client.put("/api/accel", json={"group": "xbox_cn1", "ip": "23.64.58.45"})
    r = client.post(
        "/api/resolve", json={"hostname": "assets2.xboxlive.cn", "qtype": "A"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "rule"
    assert body["ips"] == ["23.64.58.45"]
    assert body["group"] == "xbox_cn1"


def test_international_host_is_redirected_to_the_domestic_group(client):
    """assets1.xboxlive.com is served by the CN1 group, not the global one.

    The app auto-detects the local proxy address at start-up, so the
    ".com" host resolves to the proxy (which then issues the 302). What
    matters here is that the *global* (overseas) address is NOT used.
    """
    client.put("/api/accel", json={"group": "xbox_global", "ip": "1.1.1.1"})
    client.put("/api/accel", json={"group": "xbox_cn1", "ip": "2.2.2.2"})
    r = client.post(
        "/api/resolve", json={"hostname": "assets1.xboxlive.com", "qtype": "A"}
    )
    assert r.status_code == 200
    body = r.json()
    assert "1.1.1.1" not in body["ips"], "the overseas global address must not be used"
    # Either the proxy address (redirect) or the CN1 address directly.
    assert body["ips"] == ["2.2.2.2"] or body["source"] == "redirect"


def test_international_host_without_proxy_uses_the_domestic_address(client):
    """With no proxy configured, the ".com" host gets the CN1 address."""
    engine = client.app.state.services["engine"]
    engine.set_detected_proxy_ip("")
    engine.local_proxy_ip = ""
    client.put("/api/accel", json={"group": "xbox_global", "ip": "1.1.1.1"})
    client.put("/api/accel", json={"group": "xbox_cn1", "ip": "2.2.2.2"})
    r = client.post(
        "/api/resolve", json={"hostname": "assets1.xboxlive.com", "qtype": "A"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ips"] == ["2.2.2.2"]
    assert body["group"] == "xbox_cn1"


# ---------------------------------------------------------------------------
# manual host mappings
# ---------------------------------------------------------------------------
def test_add_and_list_host_mapping(client):
    r = client.post(
        "/api/hosts",
        json={"host_name": "Example.COM", "ip": "1.2.3.4", "note": "test"},
    )
    assert r.status_code == 200
    assert r.json()["host_name"] == "example.com"

    r = client.get("/api/hosts")
    assert len(r.json()["mappings"]) == 1


def test_add_host_mapping_rejects_bad_ip(client):
    r = client.post("/api/hosts", json={"host_name": "a.com", "ip": "nope"})
    assert r.status_code == 422


def test_add_host_mapping_rejects_empty_host(client):
    r = client.post("/api/hosts", json={"host_name": "", "ip": "1.2.3.4"})
    assert r.status_code == 422


def test_update_host_mapping(client):
    client.post("/api/hosts", json={"host_name": "a.com", "ip": "1.2.3.4"})
    r = client.patch("/api/hosts/0", json={"ip": "5.6.7.8", "note": "changed"})
    assert r.status_code == 200
    assert r.json()["ip"] == "5.6.7.8"
    assert r.json()["note"] == "changed"


def test_update_missing_host_mapping_is_404(client):
    r = client.patch("/api/hosts/5", json={"ip": "1.2.3.4"})
    assert r.status_code == 404


def test_delete_host_mapping(client):
    client.post("/api/hosts", json={"host_name": "a.com", "ip": "1.2.3.4"})
    assert client.delete("/api/hosts/0").status_code == 200
    assert client.get("/api/hosts").json()["mappings"] == []


def test_clear_host_mappings(client):
    client.post("/api/hosts", json={"host_name": "a.com", "ip": "1.2.3.4"})
    assert client.delete("/api/hosts").status_code == 200
    assert client.get("/api/hosts").json()["mappings"] == []


def test_manual_mapping_beats_rule(client):
    client.put("/api/accel", json={"group": "xbox_global", "ip": "23.64.58.45"})
    client.post(
        "/api/hosts", json={"host_name": "assets1.xboxlive.com", "ip": "9.9.9.9"}
    )
    r = client.post(
        "/api/resolve", json={"hostname": "assets1.xboxlive.com", "qtype": "A"}
    )
    body = r.json()
    assert body["source"] == "manual"
    assert body["ips"] == ["9.9.9.9"]


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
def test_get_settings(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    assert "upstream_dns" in r.json()


def test_update_settings(client):
    r = client.put("/api/settings", json={"speedtest_timeout_s": 42})
    assert r.status_code == 200
    assert r.json()["speedtest_timeout_s"] == 42


def test_update_settings_coerces_types(client):
    r = client.put("/api/settings", json={"dns_enabled": "yes"})
    assert r.status_code == 200
    assert r.json()["dns_enabled"] is True


def test_reset_settings(client):
    client.put("/api/settings", json={"speedtest_timeout_s": 99})
    r = client.post("/api/settings/reset")
    assert r.status_code == 200
    assert r.json()["speedtest_timeout_s"] == 10


# ---------------------------------------------------------------------------
# host rules / dns mapping / ip lists
# ---------------------------------------------------------------------------
def test_host_rules_listed(client):
    r = client.get("/api/host-rules")
    assert r.status_code == 200
    rules = r.json()["rules"]
    assert len(rules) == 8
    assert any(rule["key"] == "XboxGlobal" for rule in rules)


def test_dns_mapping_endpoint(client):
    r = client.get("/api/dns-mapping", params={"key": "XboxCn1", "ip": "1.2.3.4"})
    assert r.status_code == 200
    assert "1.2.3.4 assets1.xboxlive.cn" in r.json()["content"]


def test_dns_mapping_requires_ip(client):
    r = client.get("/api/dns-mapping", params={"key": "XboxCn1"})
    assert r.status_code == 422


def test_ip_lists_available(client):
    r = client.get("/api/ip-lists")
    assert r.status_code == 200
    pools = r.json()["pools"]
    assert any(p["key"] == "Akamai" for p in pools)
    assert all(p["count"] > 0 for p in pools)


def test_ip_pool_detail(client):
    r = client.get("/api/ip-lists/Akamai", params={"limit": 5})
    assert r.status_code == 200
    assert r.json()["total"] > 0
    assert len(r.json()["candidates"]) == 5


# ---------------------------------------------------------------------------
# speed test
# ---------------------------------------------------------------------------
def test_speedtest_targets(client):
    r = client.get("/api/speedtest/targets")
    assert r.status_code == 200
    assert "akamai" in r.json()["targets"]


def test_speedtest_rejects_empty_request(client):
    """No candidates and an unknown group must be reported, not silently pass."""
    r = client.post("/api/speedtest", json={"group": "xbox_global", "limit": 0})
    assert r.status_code == 422


def test_speedtest_with_unreachable_ip_returns_error_rows(client):
    """An unroutable address must come back as a failed row, not an error."""
    r = client.post(
        "/api/speedtest",
        json={
            "group": "akamai",
            "ips": ["192.0.2.1"],  # TEST-NET-1, guaranteed unroutable
            "target": "http://xvcf1.xboxlive.com/Z/routing/extraextralarge.txt",
            "with_ttl": False,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["ok"] is False
    assert body["results"][0]["error"]
    assert body["best"] is None


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------
def test_logs_endpoint(client):
    r = client.get("/api/logs")
    assert r.status_code == 200
    assert "lines" in r.json()
