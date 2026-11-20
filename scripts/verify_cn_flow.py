"""Verify the full acceleration chain: .com -> proxy -> 302 -> .cn -> domestic IP.

Usage: python scripts/verify_cn_flow.py [base_url] [dns_port] [http_port]
"""

import json
import socket
import subprocess
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
DNS_PORT = sys.argv[2] if len(sys.argv) > 2 else "5353"
HTTP_PORT = sys.argv[3] if len(sys.argv) > 3 else "8081"


def api(path, payload=None, method=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method or ("POST" if data else "GET")
    )
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def dig(host):
    """Resolve ``host`` through the container's DNS server."""
    out = subprocess.run(
        ["dig", "@127.0.0.1", "-p", DNS_PORT, host, "+short", "+time=10"],
        capture_output=True, text=True, timeout=30,
    )
    lines = [l.strip() for l in out.stdout.splitlines() if l.strip()]
    return lines[0] if lines else "(none)"


def http_redirect(host, path="/x"):
    """Request through the HTTP acceleration listener and report the redirect."""
    sock = socket.create_connection(("127.0.0.1", int(HTTP_PORT)), timeout=30)
    try:
        sock.sendall(
            f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()
        )
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        status = head.splitlines()[0] if head else "(no response)"
        location = ""
        for line in head.splitlines():
            if line.lower().startswith("location:"):
                location = line.split(":", 1)[1].strip()
        return status, location
    finally:
        sock.close()


print("=" * 68)
print("1) 国际域名 (.com)  ->  本地代理  ->  302  ->  国内域名 (.cn)")
print("=" * 68)
for host in ("assets1.xboxlive.com", "xvcf1.xboxlive.com", "dlassets.xboxlive.com"):
    status, location = http_redirect(host)
    print(f"  {host:<26} {status:<16} -> {location}")

print()
print("=" * 68)
print("2) DNS 解析结果")
print("=" * 68)
for host in (
    "assets1.xboxlive.com",
    "assets2.xboxlive.cn",
    "dlassets.xboxlive.com",
    "dlassets2.xboxlive.cn",
):
    print(f"  {host:<26} -> {dig(host)}")

print()
print("=" * 68)
print("3) 测速：国内组 vs 国际组")
print("=" * 68)
for group in ("xbox_cn1", "xbox_cn2", "xbox_global"):
    res = api("/api/speedtest", {"group": group, "limit": 3, "with_ttl": True})
    print(f"  [{group}] target={res['target']}")
    for r in res["results"]:
        loc = r["location"] or "-"
        print(
            f"      {r['ip']:<16} {loc:<20} "
            f"speed={r['speed_mib_s']} MiB/s  lat={r['latency_ms']} ms  ttl={r['ttl']}"
        )
    if res["best"]:
        print(f"      最快: {res['best']['ip']}  {res['best']['speed_mib_s']} MiB/s")

print()
print("=" * 68)
print("4) 加速地址配置")
print("=" * 68)
for g in api("/api/accel")["groups"]:
    if g["accel_ip"]:
        print(f"  {g['group']:<14} {g['accel_ip']:<18} ({g['label']})")
