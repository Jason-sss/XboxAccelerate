"""Tests for the certificate domain matching rules.

Ported from ``CertificateHostMatches`` / ``CertificateHostPatternMatches``
in the C# original.
"""

from __future__ import annotations

import pytest

from app.core.cert_rules import (
    CertificatePolicy,
    CertDomainMap,
    certificate_host_matches,
    normalise_host,
    pattern_matches,
)


def test_normalise_host():
    assert normalise_host("  Example.COM.  ") == "example.com"


@pytest.mark.parametrize(
    "host,pattern,expected",
    [
        # pattern_matches only handles wildcard patterns; exact matches are
        # handled by certificate_host_matches.
        ("example.com", "*.example.com", False),
        ("a.example.com", "*.example.com", True),
        ("a.b.example.com", "*.example.com", True),
        ("example.com", "*example.com", True),
        ("a.example.com", "*example.com", True),
        ("notexample.com", "*example.com", False),
        ("example.com", "other.com", False),
        ("example.com", "example.com", False),
    ],
)
def test_pattern_matches(host, pattern, expected):
    assert pattern_matches(host, pattern) is expected


@pytest.mark.parametrize(
    "host,cert,expected",
    [
        ("assets1.xboxlive.com", "assets1.xboxlive.com", True),
        ("assets1.xboxlive.com", "*.xboxlive.com", True),
        ("assets1.xboxlive.com", "*.akamaihd.net", False),
    ],
)
def test_certificate_host_matches(host, cert, expected):
    assert certificate_host_matches(host, cert) is expected


def test_certificate_host_matches_with_wildcard_host():
    """A wildcard host matches a certificate for the covered domain."""
    assert certificate_host_matches("*.xboxlive.com", "assets1.xboxlive.com") is True


# ---------------------------------------------------------------------------
# CertificatePolicy
# ---------------------------------------------------------------------------
def test_policy_accepts_exact_certificate():
    policy = CertificatePolicy(target_host="a.example.com")
    cert = {"subjectAltName": (("DNS", "a.example.com"),)}
    ok, names = policy.verify(cert=cert)
    assert ok is True
    assert names == ["a.example.com"]


def test_policy_accepts_wildcard_certificate():
    policy = CertificatePolicy(target_host="a.example.com")
    cert = {"subjectAltName": (("DNS", "*.example.com"),)}
    ok, _ = policy.verify(cert=cert)
    assert ok is True


def test_policy_rejects_mismatched_certificate():
    policy = CertificatePolicy(target_host="a.example.com")
    cert = {"subjectAltName": (("DNS", "*.other.com"),)}
    ok, names = policy.verify(cert=cert)
    assert ok is False
    assert names == ["*.other.com"]
    assert "Certificate domain mismatch" in policy.error_message(names)


def test_policy_accepts_expected_hosts():
    """The expected-hosts list allows a related certificate name."""
    policy = CertificatePolicy(
        target_host="a.xboxlive.com",
        expected_hosts=("*.akamaihd.net",),
    )
    cert = {"subjectAltName": (("DNS", "*.akamaihd.net"),)}
    ok, _ = policy.verify(cert=cert)
    assert ok is True


def test_policy_error_message_mentions_expected_hosts():
    policy = CertificatePolicy(
        target_host="a.xboxlive.com", expected_hosts=("*.akamaihd.net",)
    )
    message = policy.error_message(["*.other.com"])
    assert "expected domain" in message
    assert "*.akamaihd.net" in message


def test_policy_with_no_certificate_fails():
    policy = CertificatePolicy(target_host="a.example.com")
    ok, names = policy.verify()
    assert ok is False
    assert names == []


# ---------------------------------------------------------------------------
# CertDomainMap
# ---------------------------------------------------------------------------
def test_cert_domain_map_parses_shipped_file():
    from app.core.config import DATA_DIR

    mapping = CertDomainMap()
    mapping.load_file(DATA_DIR / "hosts" / "certdomain-mapping.txt")
    rules = mapping.rules()
    assert rules, "the shipped mapping file should not be empty"
    # The Xbox hosts must accept the Akamai certificate.
    expected = mapping.expected_hosts("assets1.xboxlive.com")
    assert "*.akamaihd.net" in expected


def test_cert_domain_map_exact_lookup():
    mapping = CertDomainMap()
    mapping.load_text('"a.example.com" = ["*.a.example.com"]')
    assert mapping.expected_hosts("a.example.com") == ("*.a.example.com",)


def test_cert_domain_map_wildcard_lookup():
    mapping = CertDomainMap()
    mapping.load_text('"*.example.com" = ["*.example.com"]')
    assert mapping.expected_hosts("a.example.com") == ("*.example.com",)
    assert mapping.expected_hosts("a.b.example.com") == ("*.example.com",)


def test_cert_domain_map_unknown_host():
    mapping = CertDomainMap()
    assert mapping.expected_hosts("nothing.invalid") == ()


def test_cert_domain_map_ignores_malformed_lines():
    mapping = CertDomainMap()
    mapping.load_text("# comment\nnot a rule\n\"a.example.com\" = [\"x\"]\n")
    assert mapping.expected_hosts("a.example.com") == ("x",)
