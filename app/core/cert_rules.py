"""Certificate domain verification for the HTTPS acceleration relay.

When the proxy pins a connection to an explicitly chosen CDN edge IP, the
certificate that edge presents is often issued for a *different* hostname
than the one the client asked for. An Akamai edge, for example, presents
a ``*.akamaihd.net`` certificate even when it serves
``assets1.xboxlive.com``.

The original desktop application handled this with two mechanisms:

1. a per-domain **certificate domain mapping** (``CertDomain.txt``) that
   declares which certificate names are acceptable for a given host, and
2. a **wildcard matching** routine that compares a host against the DNS
   names in the peer certificate.

Both are ported here.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from app.core.host_rules import normalise_host


def pattern_matches(host: str, pattern: str) -> bool:
    """Return True when ``host`` matches the certificate ``pattern``.

    Mirrors ``CertificateHostPatternMatches`` in the C# original:

    * ``*.example.com`` matches ``a.example.com`` but not ``example.com``
    * ``*.example.com`` also matches the bare ``example.com`` when the
      pattern is written as ``*example.com`` (single-star form)
    """
    if not pattern.startswith("*"):
        return False

    if pattern.startswith("*."):
        suffix = pattern[1:]
        return host.endswith(suffix) and len(host) > len(suffix)

    root_host = pattern[1:]
    if host.startswith("*."):
        host = host[2:]
    return host == root_host or host.endswith("." + root_host)


@lru_cache(maxsize=8192)
def certificate_host_matches(host: str, certificate_host: str) -> bool:
    """Return True when ``certificate_host`` is valid for ``host``."""
    host = normalise_host(host)
    certificate_host = normalise_host(certificate_host)
    if not host or not certificate_host:
        return False
    if host == certificate_host:
        return True
    if pattern_matches(host, certificate_host):
        return True
    # The host may itself be a wildcard pattern.
    return host.startswith("*") and pattern_matches(certificate_host, host)


def certificate_dns_names(cert: dict | None) -> list[str]:
    """Extract the DNS names from a parsed peer certificate.

    ``ssl.SSLSocket.getpeercert()`` returns a dict whose ``subjectAltName``
    entry holds the DNS names as ``(type, value)`` tuples.
    """
    if not cert:
        return []
    names: list[str] = []
    for entry in cert.get("subjectAltName") or ():
        if isinstance(entry, (tuple, list)) and len(entry) >= 2:
            if entry[0] == "DNS":
                names.append(str(entry[1]))
    if not names:
        # Fall back to the common name.
        for rdn in cert.get("subject") or ():
            for attr in rdn or ():
                if len(attr) >= 2 and attr[0] == "commonName":
                    names.append(str(attr[1]))
    return names


def dns_names_from_der(der: bytes | None) -> list[str]:
    """Extract the DNS names from a DER-encoded certificate.

    ``getpeercert()`` returns an empty dict when verification is disabled,
    so the binary form has to be parsed to inspect the certificate.
    """
    if not der:
        return []
    try:
        from cryptography import x509

        certificate = x509.load_der_x509_certificate(der)
    except Exception:  # pragma: no cover - defensive
        return []
    try:
        san = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
        names = list(san.get_values_for_type(x509.DNSName))
    except Exception:
        names = []
    if not names:
        try:
            common = certificate.subject.get_attributes_for_oid(
                x509.oid.NameOID.COMMON_NAME
            )
            names = [str(a.value) for a in common]
        except Exception:
            names = []
    return names


@dataclass
class CertificatePolicy:
    """Decides whether an upstream certificate is acceptable.

    ``expected_hosts`` mirrors the ``expectedHosts`` list of the C#
    original: when it is non-empty the certificate may be issued for any
    of those names instead of the requested host.
    """

    target_host: str
    expected_hosts: tuple[str, ...] = field(default_factory=tuple)

    def verify(
        self, cert: dict | None = None, der: bytes | None = None
    ) -> tuple[bool, list[str]]:
        """Return ``(ok, dns_names)`` for the given peer certificate.

        Either the parsed dict (``getpeercert()``) or the DER bytes
        (``getpeercert(binary_form=True)``) may be supplied. The DER form
        is required when the TLS context does not verify the peer.
        """
        dns_names = certificate_dns_names(cert)
        if not dns_names:
            dns_names = dns_names_from_der(der)
        if not dns_names:
            return False, []

        matched = False
        for dns in dns_names:
            if certificate_host_matches(self.target_host, dns):
                matched = True
                break

        if not matched and self.expected_hosts:
            for expected in self.expected_hosts:
                if any(
                    certificate_host_matches(expected, dns) for dns in dns_names
                ):
                    matched = True
                    break

        return matched, dns_names

    def error_message(self, dns_names: list[str]) -> str:
        """Build the operator-facing mismatch message."""
        issued = "[" + ", ".join(f'"{n}"' for n in dns_names) + "]"
        if self.expected_hosts:
            expected = "[" + ", ".join(f'"{n}"' for n in self.expected_hosts) + "]"
            return (
                "Certificate domain mismatch. "
                f"The server's certificate is issued for {issued}, "
                f"but the expected domain was {expected}."
            )
        return (
            "Certificate domain mismatch. "
            f"The server's certificate is issued for {issued}. "
            'To trust this certificate, add the following rule to the '
            f'verification policy: "{self.target_host}" = {issued}'
        )


def insecure_client_context() -> ssl.SSLContext:
    """A client context that does not verify the upstream certificate.

    The certificate is inspected manually afterwards (see
    :class:`CertificatePolicy`), which is what allows a pinned edge IP to
    present a certificate for a related hostname.
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


# ---------------------------------------------------------------------------
# Certificate domain mapping
# ---------------------------------------------------------------------------
class CertDomainMap:
    """Maps a host to the certificate names that are acceptable for it.

    The mapping file uses the same format as the C# original::

        "*github.com" = ["*github.com", "*githubusercontent.com"]

    Keys may start with ``*`` (wildcard). Values are the acceptable
    certificate names.
    """

    def __init__(self) -> None:
        self._exact: dict[str, tuple[str, ...]] = {}
        self._wildcards: list[tuple[str, tuple[str, ...]]] = []

    def load_text(self, text: str) -> None:
        """Parse a mapping document."""
        import json
        import re

        pattern = re.compile(r'^\s*"?([^"=]+)"?\s*=\s*(\[.*\])\s*$')
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = pattern.match(stripped)
            if not match:
                continue
            key = match.group(1).strip().lower()
            try:
                values = json.loads(match.group(2))
            except json.JSONDecodeError:
                continue
            if not isinstance(values, list):
                continue
            names = tuple(str(v).strip().lower() for v in values if str(v).strip())
            if key.startswith("*"):
                key = key[1:]
                if not key.startswith("."):
                    self._exact.setdefault(key, names)
                    key = "." + key
                self._wildcards.append((key, names))
            else:
                self._exact.setdefault(key, names)

        # Longest wildcard first so the most specific rule wins.
        self._wildcards.sort(key=lambda kv: len(kv[0]), reverse=True)

    def load_file(self, path) -> None:
        """Parse a mapping file, ignoring errors."""
        try:
            self.load_text(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError):
            pass

    def expected_hosts(self, host: str) -> tuple[str, ...]:
        """Return the acceptable certificate names for ``host``."""
        host = normalise_host(host)
        exact = self._exact.get(host)
        if exact is not None:
            return exact
        for suffix, names in self._wildcards:
            if host.endswith(suffix):
                return names
        return ()

    def add(self, host: str, names: tuple[str, ...]) -> None:
        """Add or replace a mapping rule."""
        key = normalise_host(host)
        self._exact[key] = names

    def rules(self) -> dict[str, tuple[str, ...]]:
        """Return every mapping rule."""
        result = dict(self._exact)
        for suffix, names in self._wildcards:
            result["*" + suffix] = names
        return result
