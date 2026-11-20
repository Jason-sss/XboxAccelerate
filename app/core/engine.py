"""The acceleration engine.

This is the heart of the refactor: it decides, for a given hostname, which
IP the connection should go to.

Resolution order (highest priority first):

1. **Manual host mapping** — an entry the operator added by hand.
2. **Built-in service rule** — the hostname belongs to a known service
   (Xbox, PlayStation, ...) and that service has an acceleration address
   configured.
3. **Blacklist** — the hostname is explicitly blocked (``0.0.0.0``).
4. **Upstream** — normal DNS resolution, unchanged.

The per-group acceleration address is the "加速地址" the operator sets in the
web UI.
"""

from __future__ import annotations

import ipaddress
import threading
from dataclasses import dataclass, field
from typing import Any

from app.core import host_rules
from app.core.host_mappings import HostMappingStore
from app.core.resolver import TYPE_A, TYPE_AAAA, UpstreamResolver


@dataclass
class Resolution:
    """The outcome of resolving one hostname."""

    hostname: str
    ips: list[str] = field(default_factory=list)
    source: str = "upstream"  # manual | rule | blacklist | upstream | cached
    rule_key: str | None = None
    group: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "hostname": self.hostname,
            "ips": self.ips,
            "source": self.source,
            "rule_key": self.rule_key,
            "group": self.group,
            "error": self.error,
        }


class AccelerationEngine:
    """Resolves hostnames, applying acceleration addresses where configured."""

    def __init__(
        self,
        settings,
        host_mappings: HostMappingStore | None = None,
        resolver: UpstreamResolver | None = None,
    ) -> None:
        self.settings = settings
        self.host_mappings = host_mappings or HostMappingStore()
        self.resolver = resolver or UpstreamResolver()
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[float, list[str]]] = {}
        self.cache_ttl = 60.0
        # The address of the local HTTP/HTTPS proxy. International Xbox
        # hosts are resolved to it so the client receives the redirect to
        # the domestic host. Empty means "resolve normally".
        self.local_proxy_ip: str = ""
        # Auto-detected fallback, used when the setting is empty. It is
        # detected once at start-up and must survive a settings refresh.
        self._detected_proxy_ip: str = ""
        # "显示 Xbox 游戏下载链接" — when off, the "1" Xbox endpoints keep
        # their own number instead of being redirected to the "2" ones.
        self.game_links_shown: bool = True

    def set_detected_proxy_ip(self, address: str) -> None:
        """Record the auto-detected proxy address."""
        self._detected_proxy_ip = str(address or "").strip()

    # -- configuration -----------------------------------------------------
    def refresh(self) -> None:
        """Re-read settings that affect resolution and drop the cache."""
        data = self.settings.all()
        self.resolver.upstream_dns = list(data.get("upstream_dns") or [])
        self.resolver.doh_servers = list(data.get("doh_servers") or [])
        self.resolver.allow_system_fallback = bool(
            data.get("allow_system_dns_fallback", True)
        )
        self.game_links_shown = bool(
            data.get("is_xbox_game_download_links_shown", True)
        )
        self.local_proxy_ip = str(data.get("local_proxy_ip") or "").strip()
        if not self.local_proxy_ip:
            # Fall back to the address detected at start-up.
            self.local_proxy_ip = self._detected_proxy_ip
        with self._lock:
            self._cache.clear()

    def accel_addresses(self) -> dict[str, str]:
        return self.settings.accel_addresses()

    def _group_for_rule(self, rule_key: str) -> str | None:
        """Map a host-rule key onto an acceleration group."""
        return host_rules.RULE_GROUPS.get(rule_key)

    # -- resolution --------------------------------------------------------
    async def resolve(
        self,
        hostname: str,
        qtype: int = TYPE_A,
        *,
        use_cache: bool = True,
    ) -> Resolution:
        """Resolve ``hostname`` to a list of IPs using the acceleration rules."""
        host = hostname.strip().lower().rstrip(".")
        if not host:
            return Resolution(hostname=hostname, error="empty hostname")

        # 1. Manual mapping — highest priority.
        manual = self.host_mappings.lookup(host)
        if manual is not None:
            return Resolution(
                hostname=host,
                ips=[manual.ip],
                source="manual",
            )

        # 2. International Xbox hosts.
        #
        #    These are redirected to their domestic equivalents BEFORE the
        #    generic service rule is applied. The ".com" hosts also belong
        #    to the XboxGlobal rule, but the global address is an overseas
        #    CDN node — using it would defeat the whole point of the
        #    accelerator.
        #
        #    When the local proxy address is known, the ".com" hosts are
        #    answered with it so the client connects to the proxy and
        #    receives the 302 redirect to the domestic host. This mirrors
        #    the C# original, which maps the ".com" hosts to the local IP
        #    (``localIpRecords``) while the ".cn" hosts get the real
        #    acceleration address.
        #
        #    Without a proxy the ".com" host is answered with the
        #    domestic acceleration address directly.
        cn_host = host_rules.cn_redirect_target(host, self.game_links_shown)
        cn_group = host_rules.cn_group_for_host(host, self.game_links_shown)
        if cn_host and cn_group:
            if self.local_proxy_ip:
                return Resolution(
                    hostname=host,
                    ips=[self.local_proxy_ip],
                    source="redirect",
                    rule_key=cn_host,
                    group=cn_group,
                )
            cn_ip = self.settings.accel_address(cn_group)
            if cn_ip:
                return Resolution(
                    hostname=host,
                    ips=[cn_ip],
                    source="rule",
                    rule_key=cn_host,
                    group=cn_group,
                )

        # 3. Built-in service rule with a configured acceleration address.
        match = host_rules.rule_for_host(host)
        if match is not None:
            bucket, rule = match
            group = self._group_for_rule(rule.key)
            if bucket == "blacklist":
                return Resolution(
                    hostname=host,
                    ips=[host_rules.BLACKLIST_IP],
                    source="blacklist",
                    rule_key=rule.key,
                    group=group,
                )
            accel_ip = self.settings.accel_address(group) if group else ""
            if accel_ip:
                return Resolution(
                    hostname=host,
                    ips=[accel_ip],
                    source="rule",
                    rule_key=rule.key,
                    group=group,
                )

        # 4. Upstream resolution (cached briefly).
        if use_cache:
            cached = self._cache_get(host, qtype)
            if cached is not None:
                return Resolution(
                    hostname=host,
                    ips=cached,
                    source="cached",
                    rule_key=match[1].key if match else None,
                    group=self._group_for_rule(match[1].key) if match else None,
                )

        want_v6 = qtype == TYPE_AAAA
        ips = await self.resolver.resolve_all(host, want_ipv6=want_v6)
        if not ips and qtype != TYPE_A:
            ips = await self.resolver.resolve_all(host, want_ipv6=False)

        self._cache_put(host, qtype, ips)
        return Resolution(
            hostname=host,
            ips=ips,
            source="upstream",
            rule_key=match[1].key if match else None,
            group=self._group_for_rule(match[1].key) if match else None,
        )

    # -- cache -------------------------------------------------------------
    def _cache_get(self, host: str, qtype: int) -> list[str] | None:
        import time

        key = f"{qtype}:{host}"
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            stamp, ips = entry
            if time.monotonic() - stamp > self.cache_ttl:
                self._cache.pop(key, None)
                return None
            return list(ips)

    def _cache_put(self, host: str, qtype: int, ips: list[str]) -> None:
        import time

        with self._lock:
            if len(self._cache) > 4096:
                self._cache.clear()
            self._cache[f"{qtype}:{host}"] = (time.monotonic(), list(ips))

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    # -- helpers -----------------------------------------------------------
    def is_valid_accel_ip(self, value: str) -> bool:
        try:
            ipaddress.ip_address(value.strip())
        except ValueError:
            return False
        return True

    def effective_mapping(self, group: str) -> dict[str, Any]:
        """Describe the current acceleration configuration of one group."""
        rule_key = host_rules.rule_for_group(group) or ""
        rule = host_rules.HOST_RULES.get(rule_key)
        ip = self.settings.accel_address(group)
        return {
            "group": group,
            "label": host_rules.HOST_RULES[rule_key].description
            if rule
            else group,
            "rule_key": rule_key,
            "accel_ip": ip,
            "configured": bool(ip) and self.is_valid_accel_ip(ip),
            "hosts": list(rule.hosts) if rule else [],
            "redirects": list(rule.redirects) if rule else [],
            "blacklist": list(rule.blacklist) if rule else [],
        }
