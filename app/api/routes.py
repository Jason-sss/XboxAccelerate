"""HTTP API routes.

The web UI talks to these endpoints. Every route is JSON; the static
frontend is mounted separately at ``/``.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.schemas import (
    AccelAddressUpdate,
    HostMappingCreate,
    HostMappingUpdate,
    ResolveRequest,
    ResolveResponse,
    SettingsUpdate,
    SpeedTestRequest,
    SpeedTestResponse,
)
from app.core import host_rules, ip_lists
from app.core.config import ACCEL_GROUP_FIELDS, ACCEL_GROUP_LABELS, ACCEL_GROUPS
from app.core.host_mappings import HostMappingStore
from app.core.resolver import TYPE_A, TYPE_AAAA
from app.core.speedtest import SPEEDTEST_TARGETS, measure_addresses

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


def _deps(request: Request) -> dict[str, Any]:
    return request.app.state.services


# ---------------------------------------------------------------------------
# Service state
# ---------------------------------------------------------------------------
@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    """Liveness probe used by Docker's HEALTHCHECK."""
    services = _deps(request)
    return {
        "status": "ok",
        "dns_running": services["dns"].running if services.get("dns") else False,
        "proxy_running": services["proxy"].running if services.get("proxy") else False,
        "version": "1.0.0",
    }


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    """Overall service status and counters."""
    services = _deps(request)
    settings = services["settings"]
    dns = services.get("dns")
    proxy = services.get("proxy")
    return {
        "settings": settings.all(),
        "accel_addresses": settings.accel_addresses(),
        "dns": {
            "running": dns.running if dns else False,
            "queries": dns.query_count if dns else 0,
            "host": dns.host if dns else None,
            "port": dns.port if dns else None,
        },
        "proxy": proxy.stats.to_dict() if proxy else {},
        "host_mappings": len(services["host_mappings"].list()),
    }


# ---------------------------------------------------------------------------
# Acceleration addresses (加速地址)
# ---------------------------------------------------------------------------
@router.get("/accel")
async def list_accel(request: Request) -> dict[str, Any]:
    """Describe the acceleration state of every group."""
    services = _deps(request)
    engine = services["engine"]
    return {
        "groups": [engine.effective_mapping(group) for group in ACCEL_GROUPS],
        "labels": ACCEL_GROUP_LABELS,
    }


@router.put("/accel")
async def set_accel(payload: AccelAddressUpdate, request: Request) -> dict[str, Any]:
    """Set the acceleration address of one group."""
    services = _deps(request)
    settings = services["settings"]
    engine = services["engine"]

    ip = payload.ip.strip()
    if ip:
        try:
            import ipaddress

            ipaddress.ip_address(ip)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid IP address: {ip}")

    field = ACCEL_GROUP_FIELDS[payload.group][0]
    settings.update({field: ip})
    engine.refresh()
    log.info("acceleration address for %s set to %r", payload.group, ip)
    return engine.effective_mapping(payload.group)


@router.post("/accel/clear")
async def clear_accel(request: Request) -> dict[str, Any]:
    """Clear every acceleration address."""
    services = _deps(request)
    services["settings"].update(
        {field: "" for field, _ in ACCEL_GROUP_FIELDS.values()}
    )
    services["engine"].refresh()
    return {"cleared": True}


# ---------------------------------------------------------------------------
# Manual host mappings
# ---------------------------------------------------------------------------
@router.get("/hosts")
async def list_hosts(request: Request) -> dict[str, Any]:
    """List the manual hostname -> IP mappings."""
    store: HostMappingStore = _deps(request)["host_mappings"]
    return {"mappings": [m.to_dict() for m in store.list()]}


@router.post("/hosts")
async def add_host(payload: HostMappingCreate, request: Request) -> dict[str, Any]:
    """Add (or replace) a manual mapping."""
    store: HostMappingStore = _deps(request)["host_mappings"]
    try:
        entry = store.add(payload.host_name, payload.ip, payload.note, payload.is_enabled)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    _deps(request)["engine"].clear_cache()
    return entry.to_dict()


@router.patch("/hosts/{index}")
async def update_host(
    index: int, payload: HostMappingUpdate, request: Request
) -> dict[str, Any]:
    """Update an existing mapping."""
    store: HostMappingStore = _deps(request)["host_mappings"]
    try:
        entry = store.update(
            index,
            **payload.model_dump(exclude_unset=True),
        )
    except IndexError:
        raise HTTPException(status_code=404, detail="Host mapping not found")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    _deps(request)["engine"].clear_cache()
    return entry.to_dict()


@router.delete("/hosts/{index}")
async def delete_host(index: int, request: Request) -> dict[str, Any]:
    """Delete a mapping."""
    store: HostMappingStore = _deps(request)["host_mappings"]
    try:
        store.delete(index)
    except IndexError:
        raise HTTPException(status_code=404, detail="Host mapping not found")
    _deps(request)["engine"].clear_cache()
    return {"deleted": True}


@router.delete("/hosts")
async def clear_hosts(request: Request) -> dict[str, Any]:
    """Delete every mapping."""
    store: HostMappingStore = _deps(request)["host_mappings"]
    store.clear()
    _deps(request)["engine"].clear_cache()
    return {"cleared": True}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@router.get("/settings")
async def get_settings(request: Request) -> dict[str, Any]:
    """Return the current settings."""
    return _deps(request)["settings"].all()


@router.put("/settings")
async def update_settings(payload: SettingsUpdate, request: Request) -> dict[str, Any]:
    """Update the settings (partial)."""
    services = _deps(request)
    changes = payload.model_dump(exclude_unset=True)
    result = services["settings"].update(changes)
    services["engine"].refresh()
    return result


@router.post("/settings/reset")
async def reset_settings(request: Request) -> dict[str, Any]:
    """Restore the default settings."""
    services = _deps(request)
    result = services["settings"].reset()
    services["engine"].refresh()
    return result


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
@router.post("/resolve", response_model=ResolveResponse)
async def resolve(payload: ResolveRequest, request: Request) -> ResolveResponse:
    """Resolve a hostname through the acceleration rules."""
    services = _deps(request)
    qtype = TYPE_AAAA if payload.qtype.upper() == "AAAA" else TYPE_A
    result = await services["engine"].resolve(payload.hostname, qtype)
    return ResolveResponse(**result.to_dict())


# ---------------------------------------------------------------------------
# Speed test (测速)
# ---------------------------------------------------------------------------
@router.get("/speedtest/targets")
async def speedtest_targets() -> dict[str, Any]:
    """List the available speed-test targets per group."""
    return {
        "targets": {
            group: [{"label": label, "url": url} for label, url in targets]
            for group, targets in SPEEDTEST_TARGETS.items()
        }
    }


@router.post("/speedtest", response_model=SpeedTestResponse)
async def speedtest(payload: SpeedTestRequest, request: Request) -> SpeedTestResponse:
    """Measure speed and latency for a set of acceleration addresses."""
    services = _deps(request)
    settings = services["settings"]
    group = (payload.group or "akamai").strip().lower()

    # Resolve the candidate addresses.
    ips = [ip.strip() for ip in payload.ips if ip.strip()]
    locations: dict[str, str] = {}

    if not ips:
        # Use the IP pool that belongs to this group. Falling back to the
        # overseas Akamai pool would measure the wrong addresses entirely
        # (e.g. "xbox_cn1" would test Argentine/Chilean edges).
        candidates = ip_lists.load_group_candidates(group)
        if not candidates:
            pool = ip_lists.pool_for_group(group)
            detail = (
                f"没有与 '{group}' 对应的 IP 地址池"
                if pool is None
                else f"IP 地址池 '{pool}' 为空"
            )
            raise HTTPException(status_code=422, detail=detail)
        for candidate in candidates:
            if candidate.ip not in locations:
                locations[candidate.ip] = candidate.location
                ips.append(candidate.ip)

    ips = ips[: payload.limit]
    if not ips:
        raise HTTPException(status_code=422, detail="No candidate addresses to test")

    # Pick the test target.
    target = payload.target
    if not target:
        targets = SPEEDTEST_TARGETS.get(group) or SPEEDTEST_TARGETS.get("akamai")
        target = targets[0][1]

    timeout = float(settings.get("speedtest_timeout_s", 10))
    chunk = int(settings.get("speedtest_chunk_bytes", 10 * 1024 * 1024))
    concurrency = payload.concurrency or int(settings.get("speedtest_concurrency", 8))

    started = time.perf_counter()
    results = await measure_addresses(
        ips,
        target,
        timeout=timeout,
        chunk_bytes=chunk,
        concurrency=concurrency,
        locations=locations,
        group=group,
        with_ttl=payload.with_ttl,
    )
    elapsed = time.perf_counter() - started

    best = next((r.to_dict() for r in results if r.ok), None)
    return SpeedTestResponse(
        group=group,
        target=target,
        results=[r.to_dict() for r in results],
        best=best,
        elapsed_s=round(elapsed, 2),
    )


# ---------------------------------------------------------------------------
# IP pool browsing
# ---------------------------------------------------------------------------
@router.get("/ip-lists")
async def list_ip_pools() -> dict[str, Any]:
    """List the importable IP pools."""
    return {"pools": ip_lists.available_groups()}


@router.get("/ip-lists/{group}")
async def list_ip_pool(group: str, limit: int = 500) -> dict[str, Any]:
    """List the addresses in one IP pool."""
    candidates = ip_lists.load_ip_list(group)
    return {
        "group": group,
        "total": len(candidates),
        "candidates": [c.to_dict() for c in candidates[: max(1, min(limit, 5000))]],
    }


@router.get("/host-rules")
async def list_host_rules() -> dict[str, Any]:
    """List the built-in host rules."""
    return {
        "rules": [
            {
                "key": rule.key,
                "description": rule.description,
                "hosts": list(rule.hosts),
                "redirects": list(rule.redirects),
                "blacklist": list(rule.blacklist),
            }
            for rule in host_rules.HOST_RULES.values()
        ]
    }


@router.get("/dns-mapping")
async def dns_mapping(key: str = "All", ip: str = "", format: str = "hosts") -> dict[str, Any]:
    """Render a DNS mapping table for the given rule key."""
    if not ip:
        raise HTTPException(status_code=422, detail="An IP address is required")
    content = host_rules.generate_dns_mapping(key, ip, export_format=format)
    return {"key": key, "ip": ip, "format": format, "content": content}


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------
@router.get("/logs")
async def recent_logs(request: Request, limit: int = 100) -> dict[str, Any]:
    """Return the recent service log lines."""
    services = _deps(request)
    buffer = services.get("log_buffer")
    if buffer is None:
        return {"lines": []}
    return {"lines": buffer.tail(max(1, min(limit, 1000)))}


@router.get("/")
async def api_root() -> JSONResponse:
    """API index."""
    return JSONResponse(
        {
            "name": "XboxDownload-Web API",
            "version": "1.0.0",
            "endpoints": [
                "/api/health",
                "/api/status",
                "/api/accel",
                "/api/hosts",
                "/api/settings",
                "/api/resolve",
                "/api/speedtest",
                "/api/ip-lists",
                "/api/host-rules",
                "/api/dns-mapping",
            ],
        }
    )
