"""Host rules for DNS mapping.

Ported from the original C# ``DnsMappingGenerator``. Each rule groups the
hostnames of one service (Xbox, PlayStation, Nintendo, ...) into three
buckets:

``hosts``
    Direct host mappings — resolved to the configured acceleration address.
``redirects``
    Alternate domains that redirect to the acceleration target.
``blacklist``
    Domains that must be blocked (mapped to ``0.0.0.0``).
"""

from __future__ import annotations

from dataclasses import dataclass, field

BLACKLIST_IP = "0.0.0.0"


@dataclass(frozen=True)
class HostRule:
    """One service group of hostnames."""

    key: str
    description: str
    hosts: tuple[str, ...] = field(default_factory=tuple)
    redirects: tuple[str, ...] = field(default_factory=tuple)
    blacklist: tuple[str, ...] = field(default_factory=tuple)


#: Rule table — mirrors ``DnsMappingGenerator.HostRules`` in the C# original.
HOST_RULES: dict[str, HostRule] = {
    "XboxGlobal": HostRule(
        key="XboxGlobal",
        description="Xbox Global",
        hosts=(
            "assets1.xboxlive.com",
            "assets2.xboxlive.com",
            "xvcf1.xboxlive.com",
            "xvcf2.xboxlive.com",
            "d1.xboxlive.com",
            "d2.xboxlive.com",
            "dlassets.xboxlive.com",
            "dlassets2.xboxlive.com",
        ),
        redirects=(
            "5.xb.delivery.microsoft.com",
            "assets1.xboxlive.com.edgesuite.net",
            "cl-glcb907925.gcdn.co",
            "fg.microsoft.map.fastly.net",
        ),
    ),
    "XboxCn1": HostRule(
        key="XboxCn1",
        description="Xbox CN1",
        hosts=(
            "assets1.xboxlive.cn",
            "assets2.xboxlive.cn",
            "d1.xboxlive.cn",
            "d2.xboxlive.cn",
        ),
    ),
    "XboxCn2": HostRule(
        key="XboxCn2",
        description="Xbox CN2",
        hosts=(
            "dlassets.xboxlive.cn",
            "dlassets2.xboxlive.cn",
        ),
    ),
    "XboxApp": HostRule(
        key="XboxApp",
        description="MS Store App",
        hosts=(
            "dl.delivery.mp.microsoft.com",
            "tlu.dl.delivery.mp.microsoft.com",
            "1d.tlu.dl.delivery.mp.microsoft.com",
        ),
    ),
    "Ps": HostRule(
        key="Ps",
        description="Sony PlayStation",
        hosts=(
            "gst.prod.dl.playstation.net",
            "gs2.ww.prod.dl.playstation.net",
            "zeus.dl.playstation.net",
            "ares.dl.playstation.net",
        ),
    ),
    "Ns": HostRule(
        key="Ns",
        description="Nintendo Switch",
        hosts=(
            "atum.hac.lp1.d4c.nintendo.net",
            "nemof.p01.lp1.nemo.srv.nintendo.net",
            "nemof.hac.lp1.nemo.srv.nintendo.net",
            "ctest-ul.p01.lp1.ctest.srv.nintendo.net",
            "ctest-dl.p01.lp1.ctest.srv.nintendo.net",
            "ctest-ul-lp1.cdn.nintendo.net",
            "ctest-dl-lp1.cdn.nintendo.net",
        ),
        blacklist=(
            "atum-eda.hac.lp1.d4c.nintendo.net",
            "atum-4ff.hac.lp1.d4c.nintendo.net",
        ),
    ),
    "Ea": HostRule(
        key="Ea",
        description="Electronic Arts",
        hosts=("origin-a.akamaihd.net",),
        blacklist=(
            "prod.cloudflare.cdn.ea.com",
            "ssl-lvlt.cdn.ea.com",
        ),
    ),
    "Battle": HostRule(
        key="Battle",
        description="Battle.net",
        hosts=(
            "blzddist1-a.akamaihd.net",
            "downloader.battle.net",
        ),
        redirects=(
            "us.cdn.blizzard.com",
            "eu.cdn.blizzard.com",
            "kr.cdn.blizzard.com",
            "level3.blizzard.com",
            "blizzard.gcdn.cloudn.co.kr",
        ),
        blacklist=("level3.ssl.blizzard.com",),
    ),
}

#: Aliases accepted from the UI / API, mapped onto real rule keys.
_RULE_ALIASES: dict[str, tuple[str, ...]] = {
    "Akamai": ("XboxGlobal", "XboxApp", "Ps", "Ns", "Ea", "Battle"),
    "All": tuple(HOST_RULES.keys()),
}


def resolve_keys(key: str | None) -> tuple[str, ...]:
    """Resolve a rule key (or alias) into concrete rule keys."""
    if key is None or not str(key).strip():
        return tuple(HOST_RULES.keys())
    key = str(key).strip()
    if key in _RULE_ALIASES:
        return _RULE_ALIASES[key]
    if key in HOST_RULES:
        return (key,)
    return (key,)


def rule_for_host(host: str) -> tuple[str, HostRule] | None:
    """Find the rule that owns ``host``.

    Exact matches win over wildcard (``*.``) entries. Returns a
    ``(bucket, rule)`` tuple where ``bucket`` is ``hosts``, ``redirects`` or
    ``blacklist``.
    """
    host = host.strip().lower().rstrip(".")
    if not host:
        return None
    for rule in HOST_RULES.values():
        if host in rule.hosts:
            return ("hosts", rule)
        if host in rule.redirects:
            return ("redirects", rule)
        if host in rule.blacklist:
            return ("blacklist", rule)
    # Wildcard match: *.example.com covers foo.example.com
    for rule in HOST_RULES.values():
        for bucket, entries in (
            ("hosts", rule.hosts),
            ("redirects", rule.redirects),
            ("blacklist", rule.blacklist),
        ):
            for entry in entries:
                if entry.startswith("*.") and host.endswith(entry[1:]):
                    return (bucket, rule)
    return None


def generate_host_list(
    key: str | None,
    include_hosts: bool = True,
    include_redirects: bool = False,
    include_blacklist: bool = False,
) -> list[str]:
    """Collect hostnames for the given rule key(s)."""
    result: list[str] = []
    for rule_key in resolve_keys(key):
        rule = HOST_RULES.get(rule_key)
        if rule is None:
            continue
        if include_hosts:
            result.extend(rule.hosts)
        if include_redirects:
            result.extend(rule.redirects)
        if include_blacklist:
            result.extend(rule.blacklist)
    return result


def generate_dns_mapping(
    key: str,
    ip: str,
    export_format: str = "hosts",
    output_mode: str = "Map",
) -> str:
    """Render a DNS mapping table.

    ``export_format`` is ``hosts`` (plain ``ip host`` lines) or ``dnsmasq``
    (``address=/host/ip`` lines). ``output_mode`` ``Export`` adds section
    comments, ``Map`` appends a trailing ``# XboxDownload`` marker.
    """
    lines: list[str] = []
    is_export = output_mode == "Export"
    is_dnsmasq = export_format == "dnsmasq"
    keys = resolve_keys(key)

    for index, rule_key in enumerate(keys):
        rule = HOST_RULES.get(rule_key)
        if rule is None:
            lines.append(f"# Unknown host key: {rule_key}")
            continue
        if is_export:
            lines.append(f"# {rule.description}")
        for host in rule.hosts:
            lines.append(_mapping_line(host, ip, False, is_export, is_dnsmasq))
        for host in rule.blacklist:
            lines.append(_mapping_line(host, BLACKLIST_IP, True, is_export, is_dnsmasq))
        if is_export and index < len(keys) - 1:
            lines.append("")

    return "\n".join(lines) + "\n"


def _mapping_line(
    host: str, ip: str, blacked: bool, is_export: bool, is_dnsmasq: bool
) -> str:
    ip_to_use = BLACKLIST_IP if blacked else ip
    comment = "" if is_export else " # XboxDownload"
    if is_dnsmasq:
        return f"address=/{host}/{ip_to_use}{comment}"
    return f"{ip_to_use} {host}{comment}"


def all_hosts_map() -> dict[str, str]:
    """Map every hostname (all buckets) to its rule key."""
    mapping: dict[str, str] = {}
    for rule in HOST_RULES.values():
        for host in (*rule.hosts, *rule.redirects, *rule.blacklist):
            mapping[host] = rule.key
    return mapping


def normalise_host(host: str) -> str:
    """Lowercase a hostname and drop the trailing dot."""
    return host.strip().rstrip(".").lower()


#: Redirects of the international Xbox download hosts to their domestic
#: (``.cn``) equivalents.
#:
#: This is the core of the acceleration: ``assets1.xboxlive.com`` is
#: redirected to ``assets2.xboxlive.cn``, which is served by domestic
#: CDN nodes, so the download no longer leaves the country.
#:
#: The mapping mirrors the ``HttpThread`` switch in the C# original
#: (``XboxDownload/Services/TcpConnectionListener.cs``). The target
#: hosts are exactly the ones the original binds to the CN1 / CN2
#: acceleration addresses:
#:
#:   assets2.xboxlive.cn   -> XboxCn1 acceleration address
#:   dlassets2.xboxlive.cn -> XboxCn2 acceleration address
XBOX_CN_REDIRECTS: dict[str, str] = {
    # International -> domestic
    "assets1.xboxlive.com": "assets2.xboxlive.cn",
    "assets2.xboxlive.com": "assets2.xboxlive.cn",
    "xvcf1.xboxlive.com": "assets2.xboxlive.cn",
    "xvcf2.xboxlive.com": "assets2.xboxlive.cn",
    "d1.xboxlive.com": "assets2.xboxlive.cn",
    "d2.xboxlive.com": "assets2.xboxlive.cn",
    "dlassets.xboxlive.com": "dlassets2.xboxlive.cn",
    "dlassets2.xboxlive.com": "dlassets2.xboxlive.cn",
    # Domestic "1" endpoints -> domestic "2" endpoints
    "assets1.xboxlive.cn": "assets2.xboxlive.cn",
    "d1.xboxlive.cn": "assets2.xboxlive.cn",
    "dlassets.xboxlive.cn": "dlassets2.xboxlive.cn",
    # Other Microsoft delivery endpoints
    "5.xb.delivery.microsoft.com": "assets2.xboxlive.cn",
    "assets1.xboxlive.com.edgesuite.net": "assets2.xboxlive.cn",
    "cl-glcb907925.gcdn.co": "assets2.xboxlive.cn",
    "fg.microsoft.map.fastly.net": "assets2.xboxlive.cn",
}

#: Redirects that change when the "显示 Xbox 游戏下载链接" switch is off.
#:
#: The C# original keeps the international "1" endpoints on their own
#: number instead of sending them to the "2" ones
#: (``TcpConnectionListener.cs``):
#:
#:   assets1.xboxlive.com -> assets1.xboxlive.cn
#:   d1.xboxlive.com      -> d1.xboxlive.cn
#:   dlassets.xboxlive.com-> dlassets.xboxlive.cn
XBOX_CN_REDIRECTS_ALT: dict[str, str] = {
    "assets1.xboxlive.com": "assets1.xboxlive.cn",
    "xvcf1.xboxlive.com": "assets1.xboxlive.cn",
    "d1.xboxlive.com": "d1.xboxlive.cn",
    "dlassets.xboxlive.com": "dlassets.xboxlive.cn",
}

#: Hosts that are only redirected while the switch is on. The domestic "1"
#: endpoints are served as-is when it is off, so they need no redirect.
XBOX_CN_GAME_LINKS_ONLY: frozenset[str] = frozenset(
    {"assets1.xboxlive.cn", "d1.xboxlive.cn", "dlassets.xboxlive.cn"}
)


def _build_off_table() -> dict[str, str]:
    """The redirect table used while the switch is off."""
    table = dict(XBOX_CN_REDIRECTS)
    table.update(XBOX_CN_REDIRECTS_ALT)
    for host in XBOX_CN_GAME_LINKS_ONLY:
        table.pop(host, None)
    return table


#: Redirect table used while the switch is off.
XBOX_CN_REDIRECTS_WITHOUT_GAME_LINKS: dict[str, str] = _build_off_table()


def rule_for_group(group: str) -> str | None:
    """Return the rule key that belongs to an acceleration group."""
    for rule, name in RULE_GROUPS.items():
        if name == group:
            return rule
    return None


def cn_redirect_target(host: str, game_links_shown: bool = True) -> str | None:
    """Return the domestic equivalent of an international Xbox host.

    ``game_links_shown`` mirrors the "显示 Xbox 游戏下载链接" switch of the
    C# original. When it is off, the "1" endpoints keep their own number
    (``assets1.xboxlive.com`` -> ``assets1.xboxlive.cn``) instead of
    being redirected to the "2" endpoints.
    """
    host = normalise_host(host)
    table = (
        XBOX_CN_REDIRECTS
        if game_links_shown
        else XBOX_CN_REDIRECTS_WITHOUT_GAME_LINKS
    )
    target = table.get(host)
    # A host is never redirected to itself — that would loop forever.
    return target if target and target != host else None


#: Rule key -> acceleration group. The group is the unit the operator
#: configures (the "加速地址") and the unit the UI and the API address.
RULE_GROUPS: dict[str, str] = {
    "XboxGlobal": "xbox_global",
    "XboxCn1": "xbox_cn1",
    "XboxCn2": "xbox_cn2",
    "XboxApp": "xbox_app",
    "Ps": "ps",
    "Ns": "ns",
    "Ea": "ea",
    "Battle": "battle",
}


def cn_group_for_host(host: str, game_links_shown: bool = True) -> str | None:
    """Return the acceleration group that serves the domestic equivalent.

    For an international host the domestic redirect target is looked up
    first, so ``assets1.xboxlive.com`` resolves through the CN1 group.

    The group is derived from the rule that owns the domestic host —
    ``assets2.xboxlive.cn`` belongs to the XboxCn1 rule, and
    ``dlassets2.xboxlive.cn`` to XboxCn2. This mirrors the ``ipMap`` in
    the C# original (``DnsConnectionListener.cs``).

    ``game_links_shown`` must match the value passed to
    :func:`cn_redirect_target` so both agree on the target host.
    """
    host = normalise_host(host)
    target = cn_redirect_target(host, game_links_shown) or host
    match = rule_for_host(target)
    return RULE_GROUPS.get(match[1].key) if match else None
