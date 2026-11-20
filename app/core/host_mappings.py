"""User-managed host mappings (手工添加的加速地址).

These are the entries the operator adds by hand in the web UI: a hostname
plus the IP that it should resolve to. They take precedence over the
built-in service rules.
"""

from __future__ import annotations

import ipaddress
import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from app.core.config import HOST_MAPPINGS_FILE
from app.core.host_rules import normalise_host


@dataclass
class HostMapping:
    """One manual hostname -> IP mapping."""

    host_name: str
    ip: str
    is_enabled: bool = True
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _is_valid_ip(ip: str) -> bool:
    try:
        ipaddress.ip_address(ip.strip())
    except ValueError:
        return False
    return True


class HostMappingStore:
    """Thread-safe JSON-backed store for manual host mappings."""

    def __init__(self, path: Path | str = HOST_MAPPINGS_FILE) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._entries: list[HostMapping] = []
        self.load()

    def load(self) -> list[HostMapping]:
        with self._lock:
            entries: list[HostMapping] = []
            if self._path.exists():
                try:
                    raw = json.loads(self._path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    raw = []
                if isinstance(raw, list):
                    for item in raw:
                        if not isinstance(item, dict):
                            continue
                        host_name = str(item.get("host_name") or "").strip()
                        ip = str(item.get("ip") or "").strip()
                        if not host_name or not _is_valid_ip(ip):
                            continue
                        entries.append(
                            HostMapping(
                                host_name=normalise_host(host_name),
                                ip=ip,
                                is_enabled=bool(item.get("is_enabled", True)),
                                note=str(item.get("note") or ""),
                            )
                        )
            self._entries = entries
            return list(self._entries)

    def save(self) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(
                    [e.to_dict() for e in self._entries], indent=2, ensure_ascii=False
                ),
                encoding="utf-8",
            )
            tmp.replace(self._path)

    def list(self) -> list[HostMapping]:
        with self._lock:
            return list(self._entries)

    def lookup(self, host_name: str) -> HostMapping | None:
        """Return the enabled mapping for ``host_name`` (exact match)."""
        host = normalise_host(host_name)
        with self._lock:
            for entry in self._entries:
                if entry.is_enabled and entry.host_name == host:
                    return entry
        return None

    def add(self, host_name: str, ip: str, note: str = "", is_enabled: bool = True) -> HostMapping:
        host = normalise_host(host_name)
        if not host:
            raise ValueError("Host name must not be empty")
        if not _is_valid_ip(ip):
            raise ValueError(f"Invalid IP address: {ip}")
        entry = HostMapping(
            host_name=host, ip=ip.strip(), is_enabled=is_enabled, note=note.strip()
        )
        with self._lock:
            self._entries = [e for e in self._entries if e.host_name != host]
            self._entries.append(entry)
            self.save()
        return entry

    def update(self, index: int, **changes: object) -> HostMapping:
        with self._lock:
            if not 0 <= index < len(self._entries):
                raise IndexError("Host mapping index out of range")
            entry = self._entries[index]
            if "host_name" in changes:
                host = normalise_host(str(changes["host_name"]))
                if not host:
                    raise ValueError("Host name must not be empty")
                entry.host_name = host
            if "ip" in changes:
                ip = str(changes["ip"]).strip()
                if not _is_valid_ip(ip):
                    raise ValueError(f"Invalid IP address: {ip}")
                entry.ip = ip
            if "note" in changes:
                entry.note = str(changes["note"] or "").strip()
            if "is_enabled" in changes:
                entry.is_enabled = bool(changes["is_enabled"])
            self.save()
        return entry

    def delete(self, index: int) -> None:
        with self._lock:
            if not 0 <= index < len(self._entries):
                raise IndexError("Host mapping index out of range")
            self._entries.pop(index)
            self.save()

    def clear(self) -> None:
        with self._lock:
            self._entries = []
            self.save()
