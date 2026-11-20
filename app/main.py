"""XboxDownload-Web — B/S (web) refactor of the XboxDownload accelerator.

Runs a FastAPI server that exposes:
  * a management UI for setting acceleration addresses and running speed tests
  * a DNS server that applies the acceleration mapping
  * HTTP/HTTPS listeners that pin connections to the acceleration IP
"""

from __future__ import annotations

import logging
import os
import sys
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

if __package__ in (None, ""):  # pragma: no cover - direct execution support
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.routes import router as api_router
from app.core.cert_rules import CertDomainMap
from app.core.config import DATA_DIR, STATE_DIR, WEB_DIR, get_settings
from app.core.engine import AccelerationEngine
from app.core.host_mappings import HostMappingStore
from app.services.certificates import CertificateProvider
from app.services.dns_server import DnsServer
from app.services.tcp_proxy import TcpProxyService

log = logging.getLogger("xboxdownload")


class LogBuffer:
    """A bounded in-memory ring buffer of recent log lines."""

    def __init__(self, maxlen: int = 1000) -> None:
        self._lines: deque[str] = deque(maxlen=maxlen)

    def append(self, line: str) -> None:
        self._lines.append(line)

    def tail(self, count: int) -> list[str]:
        return list(self._lines)[-count:]


class _MemoryHandler(logging.Handler):
    """Feeds log records into the ring buffer."""

    def __init__(self, buffer: LogBuffer) -> None:
        super().__init__()
        self._buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._buffer.append(self.format(record))
        except Exception:  # pragma: no cover - defensive
            pass


def create_app() -> FastAPI:
    """Build the FastAPI application with all services wired up."""
    settings = get_settings()
    host_mappings = HostMappingStore()
    engine = AccelerationEngine(settings, host_mappings)
    engine.refresh()

    log_buffer = LogBuffer()
    handler = _MemoryHandler(log_buffer)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)

    services: dict[str, Any] = {
        "settings": settings,
        "host_mappings": host_mappings,
        "engine": engine,
        "dns": None,
        "proxy": None,
        "log_buffer": log_buffer,
        "certificates": None,
    }

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await _start_services(services)
        try:
            yield
        finally:
            await _stop_services(services)

    app = FastAPI(
        title="XboxDownload-Web",
        description="B/S refactor of the XboxDownload game-download accelerator",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.services = services

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router)

    _mount_web_ui(app)
    return app


def _mount_web_ui(app: FastAPI) -> None:
    """Serve the static frontend."""
    web_dir = WEB_DIR
    if not web_dir.exists():
        log.warning("web directory not found at %s", web_dir)
        return

    index = web_dir / "index.html"

    @app.get("/", include_in_schema=False)
    async def root() -> FileResponse:
        return FileResponse(index)

    app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")


def _detect_local_ip() -> str:
    """Return the primary non-loopback IPv4 address of this host.

    Used as the address the international Xbox hosts are resolved to, so
    that clients reach the local proxy and get redirected to the domestic
    host. Falls back to ``127.0.0.1``.
    """
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No packet is sent; this just asks the routing table which
        # source address would be used for the default route.
        probe.connect(("223.5.5.5", 53))
        address = probe.getsockname()[0]
        if address and not address.startswith("127."):
            return str(address)
    except OSError:
        pass
    finally:
        probe.close()
    return "127.0.0.1"


async def _start_services(services: dict[str, Any]) -> None:
    """Start the DNS server and TCP proxy if enabled in the settings."""
    settings = services["settings"]
    engine: AccelerationEngine = services["engine"]
    host_mappings: HostMappingStore = services["host_mappings"]

    # Re-read persisted state on boot.
    host_mappings.load()
    engine.refresh()

    listening_ip = str(settings.get("listening_ip") or "0.0.0.0")
    dns_port = int(settings.get("dns_port") or 53)
    http_port = int(settings.get("http_port") or 80)
    https_port = int(settings.get("https_port") or 443)

    # The international Xbox hosts are resolved to the proxy address so the
    # client receives the redirect to the domestic host. When it is not
    # configured explicitly, the address of this host is used.
    local_proxy_ip = str(settings.get("local_proxy_ip") or "").strip()
    if not local_proxy_ip and settings.get("http_enabled", True):
        local_proxy_ip = _detect_local_ip()
        engine.set_detected_proxy_ip(local_proxy_ip)
    engine.local_proxy_ip = local_proxy_ip
    if local_proxy_ip:
        log.info("international Xbox hosts resolve to the proxy at %s", local_proxy_ip)

    if settings.get("dns_enabled"):
        dns = DnsServer(engine, listening_ip, dns_port)
        try:
            await dns.start()
            services["dns"] = dns
        except OSError as exc:
            log.error("failed to start DNS server on %s:%s: %s", listening_ip, dns_port, exc)
            services["dns"] = None

    if settings.get("http_enabled") or settings.get("https_enabled"):
        provider = CertificateProvider(Path(STATE_DIR))
        services["certificates"] = provider
        try:
            provider.save_default_certificate()
        except Exception:  # pragma: no cover - defensive
            log.exception("failed to prepare the default certificate")

        # Certificate domain mapping: which certificate names are acceptable
        # for a given host when the connection is pinned to an explicit edge.
        cert_domains = CertDomainMap()
        cert_domains.load_file(DATA_DIR / "hosts" / "certdomain-mapping.txt")
        services["cert_domains"] = cert_domains
        log.info("loaded %d certificate domain rules", len(cert_domains.rules()))

        proxy = TcpProxyService(
            engine,
            host=listening_ip,
            http_port=http_port,
            https_port=https_port,
            certificate_provider=provider,
            cert_domains=cert_domains,
        )
        try:
            await proxy.start()
            services["proxy"] = proxy
        except OSError as exc:
            log.error("failed to start TCP proxy: %s", exc)
            services["proxy"] = None


async def _stop_services(services: dict[str, Any]) -> None:
    """Stop the background services."""
    dns = services.get("dns")
    if dns is not None:
        await dns.stop()
        services["dns"] = None
    proxy = services.get("proxy")
    if proxy is not None:
        await proxy.stop()
        services["proxy"] = None


app = create_app()


def main() -> None:
    """Run the web server."""
    import uvicorn

    host = os.environ.get("XBD_HTTP_HOST", "0.0.0.0")
    port = int(os.environ.get("XBD_HTTP_PORT", "8080"))
    log_level = os.environ.get("XBD_LOG_LEVEL", "info").lower()

    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        log_level=log_level,
        access_log=log_level == "debug",
    )


if __name__ == "__main__":
    main()
