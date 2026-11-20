"""Application configuration and persistence.

Settings are stored as JSON so they survive container restarts. The model
mirrors the semantics of the original C# ``AppSettings`` class so the two
implementations stay conceptually aligned.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from app.core import host_rules

# Service groups that own an "acceleration address" (加速地址).
#: Display label of every acceleration group.
ACCEL_GROUP_LABELS: dict[str, str] = {
    "xbox_global": "Xbox Global",
    "xbox_cn1": "Xbox CN1",
    "xbox_cn2": "Xbox CN2",
    "xbox_app": "MS Store App",
    "ps": "Sony PlayStation",
    "ns": "Nintendo Switch",
    "ea": "Electronic Arts",
    "battle": "Battle.net",
}

#: The acceleration groups, in display order.
ACCEL_GROUPS: tuple[str, ...] = tuple(ACCEL_GROUP_LABELS)

#: Per-group settings field and host-rule key. The rule key comes from
#: ``host_rules.RULE_GROUPS`` so the group -> rule pairing is defined once.
ACCEL_GROUP_FIELDS: dict[str, tuple[str, str]] = {
    group: (f"{group}_ip", host_rules.rule_for_group(group) or "")
    for group in ACCEL_GROUPS
}


#: Environment variables that override settings at startup. This lets an
#: operator configure a container purely from the environment.
#:
#: Note the naming: ``XBD_HTTP_PORT`` is the *management UI* port (used by
#: the uvicorn entrypoint), while ``XBD_PROXY_HTTP_PORT`` is the port of the
#: HTTP *acceleration listener*. They are different services.
ENV_OVERRIDES: dict[str, str] = {
    "XBD_LISTENING_IP": "listening_ip",
    "XBD_LOCAL_PROXY_IP": "local_proxy_ip",
    "XBD_DNS_ENABLED": "dns_enabled",
    "XBD_DNS_PORT": "dns_port",
    "XBD_PROXY_HTTP_ENABLED": "http_enabled",
    "XBD_PROXY_HTTP_PORT": "http_port",
    "XBD_PROXY_HTTPS_ENABLED": "https_enabled",
    "XBD_PROXY_HTTPS_PORT": "https_port",
    "XBD_IPV6_ENABLED": "ipv6_enabled",
    "XBD_XBOX_GLOBAL_IP": "xbox_global_ip",
    "XBD_XBOX_CN1_IP": "xbox_cn1_ip",
    "XBD_XBOX_CN2_IP": "xbox_cn2_ip",
    "XBD_XBOX_APP_IP": "xbox_app_ip",
    "XBD_PS_IP": "ps_ip",
    "XBD_NS_IP": "ns_ip",
    "XBD_EA_IP": "ea_ip",
    "XBD_BATTLE_IP": "battle_ip",
    "XBD_SPEEDTEST_TIMEOUT_S": "speedtest_timeout_s",
    "XBD_SPEEDTEST_CONCURRENCY": "speedtest_concurrency",
}

_BOOL_TRUE = {"1", "true", "yes", "on"}
_BOOL_FALSE = {"0", "false", "no", "off"}


def _env_overrides() -> dict[str, Any]:
    """Read the environment overrides into settings-shaped values."""
    overrides: dict[str, Any] = {}
    for env_name, key in ENV_OVERRIDES.items():
        raw = os.environ.get(env_name)
        if raw is None or raw == "":
            continue
        value = raw.strip()
        default = DEFAULT_SETTINGS.get(key)
        if isinstance(default, bool):
            lowered = value.lower()
            if lowered in _BOOL_TRUE:
                overrides[key] = True
            elif lowered in _BOOL_FALSE:
                overrides[key] = False
            else:
                continue
        elif isinstance(default, int):
            try:
                overrides[key] = int(value)
            except ValueError:
                continue
        else:
            overrides[key] = value
    return overrides


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = _env_path("XBD_DATA_DIR", PROJECT_ROOT / "data")
STATE_DIR = _env_path("XBD_STATE_DIR", PROJECT_ROOT / "state")
WEB_DIR = _env_path("XBD_WEB_DIR", PROJECT_ROOT / "web")

SETTINGS_FILE = STATE_DIR / "settings.json"
HOST_MAPPINGS_FILE = STATE_DIR / "host-mappings.json"

DEFAULT_UPSTREAM_DNS: tuple[str, ...] = (
    "223.5.5.5",
    "119.29.29.29",
    "1.1.1.1",
    "8.8.8.8",
)

DEFAULT_DOH_SERVERS: list[dict[str, Any]] = [
    {
        "id": "DNSPod",
        "name": "DNSPod",
        "url": "https://doh.pub/dns-query",
        "ip": "119.29.29.29",
        "proxy_disabled": False,
    },
    {
        "id": "AliDNS",
        "name": "AliDNS",
        "url": "https://dns.alidns.com/dns-query",
        "ip": "223.5.5.5",
        "proxy_disabled": False,
    },
    {
        "id": "Google",
        "name": "Google",
        "url": "https://dns.google/dns-query",
        "ip": "8.8.8.8",
        "proxy_disabled": True,
    },
    {
        "id": "Cloudflare",
        "name": "Cloudflare",
        "url": "https://cloudflare-dns.com/dns-query",
        "ip": "1.1.1.1",
        "proxy_disabled": True,
    },
]

DEFAULT_SETTINGS: dict[str, Any] = {
    # --- acceleration addresses (加速地址) -------------------------------
    "xbox_global_ip": "",
    "xbox_cn1_ip": "",
    "xbox_cn2_ip": "",
    "xbox_app_ip": "",
    "ps_ip": "",
    "ns_ip": "",
    "ea_ip": "",
    "battle_ip": "",
    # --- listeners --------------------------------------------------------
    "listening_ip": "0.0.0.0",
    # The address the international Xbox hosts are resolved to, so that
    # clients connect to the local proxy and get redirected to the
    # domestic (.cn) host. Empty disables the redirect mechanism.
    "local_proxy_ip": "",
    "dns_enabled": True,
    "dns_port": 53,
    "http_enabled": True,
    "http_port": 80,
    "https_enabled": True,
    "https_port": 443,
    "local_proxy_enabled": False,
    # --- resolution -------------------------------------------------------
    "upstream_dns": list(DEFAULT_UPSTREAM_DNS),
    "doh_servers": DEFAULT_DOH_SERVERS,
    "allow_system_dns_fallback": True,
    "sni_proxy_doh_ids": ["Google", "Cloudflare"],
    "ipv6_enabled": False,
    # --- behaviour --------------------------------------------------------
    "is_xbox_game_download_links_shown": True,
    "is_logging": True,
    "auto_select_fastest": True,
    "speedtest_timeout_s": 10,
    "speedtest_chunk_bytes": 10 * 1024 * 1024,
    "speedtest_concurrency": 8,
}

# Keys whose values must be a list of strings.
_LIST_FIELDS = ("upstream_dns", "sni_proxy_doh_ids")


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into a copy of ``base``."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class SettingsStore:
    """Thread-safe JSON-backed settings store.

    A single instance is shared by the whole application; the engine reads
    from it and the API writes to it.
    """

    def __init__(self, path: Path = SETTINGS_FILE) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = dict(DEFAULT_SETTINGS)
        self.load()

    # -- persistence -------------------------------------------------------
    def load(self) -> dict[str, Any]:
        with self._lock:
            raw: dict[str, Any] = {}
            if self._path.exists():
                try:
                    loaded = json.loads(self._path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        raw = loaded
                except (OSError, json.JSONDecodeError):
                    raw = {}
            self._data = _deep_merge(DEFAULT_SETTINGS, raw)
            # Environment variables win over the persisted file so a container
            # can be reconfigured without touching the volume.
            self._data = _deep_merge(self._data, _env_overrides())
            self._normalise()
            return dict(self._data)

    def save(self) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(self._path)

    def _normalise(self) -> None:
        """Coerce field types so a hand-edited file cannot break the engine."""
        for key in _LIST_FIELDS:
            value = self._data.get(key)
            if not isinstance(value, list):
                self._data[key] = list(DEFAULT_SETTINGS.get(key, []))
            else:
                self._data[key] = [str(v) for v in value]
        for key, default in (
            ("dns_port", 53),
            ("http_port", 80),
            ("https_port", 443),
            ("speedtest_timeout_s", 10),
            ("speedtest_chunk_bytes", 10 * 1024 * 1024),
            ("speedtest_concurrency", 8),
        ):
            try:
                self._data[key] = int(self._data.get(key, default))
            except (TypeError, ValueError):
                self._data[key] = default
        for key in (
            "dns_enabled",
            "http_enabled",
            "https_enabled",
            "local_proxy_enabled",
            "ipv6_enabled",
            "is_xbox_game_download_links_shown",
            "is_logging",
            "auto_select_fastest",
            "allow_system_dns_fallback",
        ):
            self._data[key] = bool(self._data.get(key, DEFAULT_SETTINGS[key]))
        for key in ACCEL_GROUP_FIELDS:
            self._data[key] = str(self._data.get(key, "") or "").strip()
        self._data["listening_ip"] = str(
            self._data.get("listening_ip") or "0.0.0.0"
        ).strip()

    # -- access ------------------------------------------------------------
    def all(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Apply a partial update, normalise and persist."""
        with self._lock:
            self._data = _deep_merge(self._data, changes)
            self._normalise()
            self.save()
            return json.loads(json.dumps(self._data))

    def reset(self) -> dict[str, Any]:
        with self._lock:
            self._data = dict(DEFAULT_SETTINGS)
            self._normalise()
            self.save()
            return json.loads(json.dumps(self._data))

    # -- convenience -------------------------------------------------------
    def accel_addresses(self) -> dict[str, str]:
        """Return ``{group: ip}`` for every acceleration group."""
        with self._lock:
            return {
                group: str(self._data.get(field, "") or "")
                for group, (field, _) in ACCEL_GROUP_FIELDS.items()
            }

    def accel_address(self, group: str) -> str:
        field = ACCEL_GROUP_FIELDS.get(group, (None, None))[0]
        if field is None:
            return ""
        with self._lock:
            return str(self._data.get(field, "") or "")


_settings_store: SettingsStore | None = None


def get_settings() -> SettingsStore:
    """Return the process-wide settings store (created on first use)."""
    global _settings_store
    if _settings_store is None:
        _settings_store = SettingsStore()
    return _settings_store
