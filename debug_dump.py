# -*- coding: utf-8 -*-
"""调试用：把 /api/state 拉下来打印成可读报告"""
import json
import sys
import urllib.request

PORT = sys.argv[1] if len(sys.argv) > 1 else "8899"
o = urllib.request.build_opener(urllib.request.ProxyHandler({}))
raw = o.open(f"http://127.0.0.1:{PORT}/api/state", timeout=45).read().decode("utf-8")
d = json.loads(raw)

L = []
L.append(f"ready={d.get('ready')}  ts={d.get('ts')}  bytes={len(raw)}")
if d.get("last_error"):
    L.append("!!! " + d["last_error"])
L.append("")
L.append("=== 服务 ===")
for s in d.get("services", []):
    L.append(f"{s['id']:<14} {s['state']:<9} {str(s.get('ms')):<6} pid={s.get('pids')}  {s.get('detail')}")
L.append("")
L.append("=== 映射 ===")
for m in d.get("mappings", []):
    L.append(f"[{m['id']}] {m['name']} -> {m['state']}")
    for g in m.get("segments", []):
        L.append(f"    {g['label']:<10} {g['state']:<9} {str(g.get('ms')):<6} {g.get('hint')}  | {g.get('detail')}")
    dd = m.get("ddns")
    if dd:
        L.append(f"    DDNS {dd['type']} {dd['record']}: {dd['state']} {dd.get('ms')}ms "
                 f"addrs={dd.get('addrs')} prefix_match={dd.get('prefix_match')} | {dd.get('detail')}")
L.append("")
L.append("=== 本机地址 ===")
h = d.get("host", {})
for x in h.get("v4", []):
    L.append(f"  v4 {x['iface']}: {x['addr']}")
for x in h.get("v6", []):
    L.append(f"  v6 {x['iface']}: {x['addr']} ({x['kind']})")

out = "\n".join(L)
open("_state.txt", "w", encoding="utf-8").write(out)
print("written")
