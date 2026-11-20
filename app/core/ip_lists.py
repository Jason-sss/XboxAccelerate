"""Parsing of the IP candidate lists shipped in ``data/ip``.

The source format (``IP/*.txt``) is::

    Akamai
    <blank line>
    23.64.58.45\t(Argentina Buenos Aires)
    23.196.15.225\t(Argentina Buenos Aires)

The first non-empty line is the group name, every following line is an
``<ip>\\t(<location>)`` pair. ``Translation.json`` maps the Chinese
location names to ``"CC, City"`` English forms.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from functools import lru_cache

from app.core.config import DATA_DIR

IP_DIR = DATA_DIR / "ip"

_LINE_RE = re.compile(
    r"^(?P<ip>[^\s\t]+)\s*(?:\t|\s)\s*\(?(?P<location>[^()]*?)\)?\s*$"
)

#: Logical list name -> file name under ``data/ip``.
IP_LIST_FILES: dict[str, str] = {
    "Akamai": "IP.Akamai.txt",
    "AkamaiV2": "IP.AkamaiV2.txt",
    "AkamaiV6": "IP.AkamaiV6.txt",
    "XboxCn1": "IP.XboxCn1.txt",
    "XboxCn2": "IP.XboxCn2.txt",
    "XboxApp": "IP.XboxApp.txt",
    "Ps": "IP.Ps.txt",
    "UbisoftCn": "IP.UbisoftCn.txt",
}

#: Acceleration group -> the IP pool that holds its candidate addresses.
#:
#: The group keys are the ones used by the UI and the API (``xbox_cn1``,
#: ``xbox_cn2``, ...), while the pool files use the original C# names
#: (``XboxCn1``, ``XboxCn2``, ...). Without this mapping a request for
#: ``xbox_cn1`` would silently fall back to the overseas Akamai pool.
GROUP_IP_POOLS: dict[str, str] = {
    "xbox_global": "Akamai",
    "xbox_cn1": "XboxCn1",
    "xbox_cn2": "XboxCn2",
    "xbox_app": "XboxApp",
    "ps": "Ps",
    "ns": "Akamai",
    "ea": "Akamai",
    "battle": "Akamai",
    "akamai": "Akamai",
    "akamaiv2": "AkamaiV2",
    "akamaiv6": "AkamaiV6",
    "ubisoftcn": "UbisoftCn",
}


@dataclass
class IpCandidate:
    """A single acceleration-address candidate."""

    ip: str
    location: str
    group: str
    is_ipv6: bool = False

    @property
    def location_en(self) -> str:
        return _translate_location(self.location)

    def to_dict(self) -> dict[str, object]:
        return {
            "ip": self.ip,
            "location": self.location,
            "location_en": self.location_en,
            "group": self.group,
            "is_ipv6": self.is_ipv6,
        }


@lru_cache(maxsize=1)
def _translation_table() -> dict[str, str]:
    path = IP_DIR / "Translation.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _translate_location(location: str) -> str:
    """Translate a Chinese location into ``"CC, City"`` when possible."""
    table = _translation_table()
    if location in table:
        return str(table[location])
    # Fall back to a per-token lookup for compound names.
    parts = [p for p in re.split(r"\s+", location.strip()) if p]
    if parts and parts[0] in table:
        return str(table[parts[0]])
    return location


def parse_ip_list(text: str, group: str) -> list[IpCandidate]:
    """Parse an IP list file body into candidates."""
    candidates: list[IpCandidate] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("﻿")
        if not line or line.startswith("#"):
            continue
        match = _LINE_RE.match(line)
        if not match:
            continue
        ip_text = match.group("ip").strip()
        try:
            address = ipaddress.ip_address(ip_text)
        except ValueError:
            continue
        if address.is_unspecified or address.is_multicast:
            continue
        if ip_text in seen:
            continue
        seen.add(ip_text)
        location = (match.group("location") or "").strip()
        candidates.append(
            IpCandidate(
                ip=ip_text,
                location=location,
                group=group,
                is_ipv6=address.version == 6,
            )
        )
    return candidates


def load_ip_list(group: str) -> list[IpCandidate]:
    """Load the candidate list for a logical group name."""
    filename = IP_LIST_FILES.get(group)
    if filename is None:
        return []
    path = IP_DIR / filename
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return []
    return parse_ip_list(text, group)


def pool_for_group(group: str) -> str | None:
    """Return the IP pool name that belongs to an acceleration group."""
    if not group:
        return None
    key = str(group).strip().lower()
    if key in GROUP_IP_POOLS:
        return GROUP_IP_POOLS[key]
    # Accept the pool names themselves as well.
    for pool in IP_LIST_FILES:
        if pool.lower() == key:
            return pool
    return None


def load_group_candidates(group: str) -> list[IpCandidate]:
    """Load the candidate addresses for an acceleration group.

    Unlike :func:`load_ip_list` this never silently falls back to the
    overseas Akamai pool — a group with no matching pool returns an empty
    list so the caller can report the problem instead of measuring the
    wrong addresses.
    """
    pool = pool_for_group(group)
    if pool is None:
        return []
    return load_ip_list(pool)


def available_groups() -> list[dict[str, object]]:
    """Describe every IP list available for import."""
    result: list[dict[str, object]] = []
    for group in IP_LIST_FILES:
        candidates = load_ip_list(group)
        if not candidates:
            continue
        result.append(
            {
                "key": group,
                "count": len(candidates),
                "has_ipv6": any(c.is_ipv6 for c in candidates),
                "sample": [c.to_dict() for c in candidates[:3]],
            }
        )
    return result
