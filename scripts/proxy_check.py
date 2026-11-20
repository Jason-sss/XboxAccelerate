"""Exercise the HTTP/HTTPS acceleration listeners through the container.

Usage: python scripts/proxy_check.py [http_port] [https_port]
"""

import http.client
import socket
import ssl
import sys

HTTP_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8081
HTTPS_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8443

HOST = "assets1.xboxlive.com"


def http_get(host_header, path="/Z/routing/extraextralarge.txt", port=HTTP_PORT):
    """Issue a plain HTTP request through the acceleration listener.

    The upstream file is large, so only the response head is read — the
    point is to prove the relay works, not to download the whole thing.
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request(
            "GET", path, headers={"Host": host_header, "User-Agent": "XboxDownload-Web/1.0"}
        )
        resp = conn.getresponse()
        head = resp.read(64)
        return resp.status, resp.getheader("Location"), resp.getheader("Content-Length"), head
    finally:
        conn.close()


def https_get(host_header, path="/Z/routing/extraextralarge.txt", port=HTTPS_PORT):
    """Issue a TLS request through the acceleration listener."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    conn = http.client.HTTPSConnection("127.0.0.1", port, timeout=30, context=ctx)
    try:
        conn.request(
            "GET", path, headers={"Host": host_header, "User-Agent": "XboxDownload-Web/1.0"}
        )
        resp = conn.getresponse()
        head = resp.read(64)
        return resp.status, resp.getheader("Location"), resp.getheader("Content-Length"), head
    finally:
        conn.close()


print("== HTTP listener: accelerated host (pinned to the acceleration IP) ==")
try:
    status, location, size, head = http_get(HOST)
    print(f"  status={status} location={location} bytes={size}")
    print(f"  head={head[:70]!r}")
except Exception as exc:
    print(f"  ERROR {type(exc).__name__}: {exc}")

print("\n== HTTP listener: redirect target (us.cdn.blizzard.com) ==")
try:
    status, location, size, head = http_get("us.cdn.blizzard.com", "/x")
    print(f"  status={status} location={location} bytes={size}")
except Exception as exc:
    print(f"  ERROR {type(exc).__name__}: {exc}")

print("\n== HTTP listener: unknown host (expect 404) ==")
try:
    status, location, size, head = http_get("unknown.invalid.example", "/x")
    print(f"  status={status} location={location} bytes={size}")
except Exception as exc:
    print(f"  ERROR {type(exc).__name__}: {exc}")

print("\n== HTTPS listener: TLS handshake + accelerated host ==")
try:
    status, location, size, head = https_get(HOST)
    print(f"  status={status} location={location} bytes={size}")
    print(f"  head={head[:70]!r}")
except Exception as exc:
    print(f"  ERROR {type(exc).__name__}: {exc}")

print("\n== raw TLS handshake against the HTTPS listener ==")
try:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection(("127.0.0.1", HTTPS_PORT), timeout=15) as raw:
        with ctx.wrap_socket(raw, server_hostname=HOST) as tls:
            print("  handshake OK, peer cert CN present:", tls.getpeercert() is not None)
except Exception as exc:
    print(f"  ERROR {type(exc).__name__}: {exc}")
