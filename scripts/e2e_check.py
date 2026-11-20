"""Ad-hoc end-to-end check against a running container.

Usage: python scripts/e2e_check.py [base_url]
"""

import json
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"


def call(path, payload=None, method=None):
    url = BASE + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method or ("POST" if data else "GET")
    )
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode())


print("== health ==")
print(call("/api/health"))

print("\n== accel groups (configured) ==")
accel = call("/api/accel")
for g in accel["groups"]:
    if g["accel_ip"]:
        print(f"  {g['group']:<14} {g['accel_ip']:<18} configured={g['configured']}")

print("\n== speed test: 3 real Akamai edges ==")
res = call(
    "/api/speedtest",
    {
        "group": "akamai",
        "ips": ["23.64.58.45", "23.196.15.225", "96.17.164.138"],
        "target": "http://xvcf1.xboxlive.com/Z/routing/extraextralarge.txt",
        "limit": 3,
        "with_ttl": True,
    },
)
print(f"  elapsed {res['elapsed_s']}s  target {res['target']}")
for r in res["results"]:
    print(
        f"  {r['ip']:<16} speed={r['speed_mib_s']} MiB/s  "
        f"latency={r['latency_ms']} ms  ttl={r['ttl']}  "
        f"bytes={r['bytes_downloaded']}  ok={r['ok']}  err={r['error']}"
    )
print("  best:", res["best"]["ip"] if res["best"] else None)

print("\n== resolve (acceleration applied) ==")
print(" ", call("/api/resolve", {"hostname": "assets1.xboxlive.com", "qtype": "A"}))

print("\n== resolve (manual mapping) ==")
print(" ", call("/api/resolve", {"hostname": "shop.local", "qtype": "A"}))

print("\n== status ==")
st = call("/api/status")
print("  dns:", st["dns"])
print("  proxy:", st["proxy"])
print("  host_mappings:", st["host_mappings"])
