"""Local certificate generation for the HTTPS acceleration listener.

The desktop app installed a root CA into the Windows certificate store and
minted per-host certificates. In a container we generate a self-signed CA
plus a leaf certificate on demand, so the proxy can terminate TLS without
touching the host trust store.

The CA and leaf are persisted under the state directory so the certificate
stays valid across restarts.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import logging
import threading
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

log = logging.getLogger(__name__)

CA_CERT_FILE = "ca.crt"
CA_KEY_FILE = "ca.key"
LEAF_CERT_FILE = "leaf.crt"
LEAF_KEY_FILE = "leaf.key"

DEFAULT_VALID_DAYS = 825


class CertificateProvider:
    """Generates and caches the CA + leaf certificates used by the proxy."""

    def __init__(self, state_dir: Path, domains: list[str] | None = None) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.domains = domains or ["*.akamai.net", "*.akamaihd.net", "*.akamaized.net"]
        self._lock = threading.RLock()
        self._leaf_certs: dict[str, tuple[bytes, bytes]] = {}

    # -- paths -------------------------------------------------------------
    @property
    def ca_cert_path(self) -> Path:
        return self.state_dir / CA_CERT_FILE

    @property
    def ca_key_path(self) -> Path:
        return self.state_dir / CA_KEY_FILE

    @property
    def leaf_cert_path(self) -> Path:
        return self.state_dir / LEAF_CERT_FILE

    @property
    def leaf_key_path(self) -> Path:
        return self.state_dir / LEAF_KEY_FILE

    # -- CA ----------------------------------------------------------------
    def _load_or_create_ca(self) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
        if self.ca_cert_path.exists() and self.ca_key_path.exists():
            try:
                cert = x509.load_pem_x509_certificate(self.ca_cert_path.read_bytes())
                key = serialization.load_pem_private_key(
                    self.ca_key_path.read_bytes(), password=None
                )
                if isinstance(key, rsa.RSAPrivateKey):
                    return cert, key
            except (OSError, ValueError) as exc:
                log.warning("failed to load existing CA, regenerating: %s", exc)

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name(
            [
                x509.NameAttribute(NameOID.COMMON_NAME, "XboxDownload-Web CA"),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "XboxDownload"),
            ]
        )
        now = dt.datetime.now(dt.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=DEFAULT_VALID_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(key, hashes.SHA256())
        )

        self.ca_cert_path.write_bytes(
            cert.public_bytes(serialization.Encoding.PEM)
        )
        self.ca_key_path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        os_chmod(self.ca_key_path, 0o600)
        return cert, key

    # -- leaf --------------------------------------------------------------
    def _issue_leaf(self, hostname: str) -> tuple[bytes, bytes]:
        ca_cert, ca_key = self._load_or_create_ca()
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        try:
            ip = ipaddress.ip_address(hostname)
            san: x509.GeneralName = x509.IPAddress(ip)
        except ValueError:
            san = x509.DNSName(hostname)

        now = dt.datetime.now(dt.timezone.utc)
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname[:64])]))
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.SubjectAlternativeName([san]),
                critical=False,
            )
            .add_extension(
                x509.ExtendedKeyUsage(
                    [
                        x509.oid.ExtendedKeyUsageOID.SERVER_AUTH,
                        x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH,
                    ]
                ),
                critical=False,
            )
        )
        cert = builder.sign(ca_key, hashes.SHA256())

        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        key_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return cert_pem, key_pem

    # -- public API --------------------------------------------------------
    def get_certificate(self, hostname: str = "xboxdownload.local") -> tuple[bytes, bytes]:
        """Return ``(cert_pem, key_pem)`` valid for ``hostname``."""
        hostname = (hostname or "xboxdownload.local").strip().lower()
        with self._lock:
            cached = self._leaf_certs.get(hostname)
            if cached is not None:
                return cached
            issued = self._issue_leaf(hostname)
            self._leaf_certs[hostname] = issued
            if len(self._leaf_certs) > 512:
                self._leaf_certs.clear()
            return issued

    def save_default_certificate(self) -> tuple[Path, Path]:
        """Persist a default leaf certificate for the main listener."""
        cert_pem, key_pem = self.get_certificate()
        self.leaf_cert_path.write_bytes(cert_pem)
        self.leaf_key_path.write_bytes(key_pem)
        os_chmod(self.leaf_key_path, 0o600)
        return self.leaf_cert_path, self.leaf_key_path

    def certificate_files(self) -> tuple[Path, Path]:
        """Return on-disk paths to a usable certificate/key pair.

        ``ssl.SSLContext.load_cert_chain`` requires file paths, so the
        default leaf is written to the state directory and re-used.
        """
        if not self.leaf_cert_path.exists() or not self.leaf_key_path.exists():
            self.save_default_certificate()
        return self.leaf_cert_path, self.leaf_key_path


def os_chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass
