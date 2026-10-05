import json, math, re, subprocess, sys, time
RX = re.compile(r'^(\S+) \S+ \S+ \[[^\]]+\] "(\S+) (\S+) [^"]*" (\d{3}) (\d+) "[^"]*" "[^"]*"$')
total = mal = 0; st = {}; paths = {}; sizes = []; ips = set()
for ln in open("access.log").read().split("\n")[:-1]:
    m = RX.match(ln)
    if not m:
        mal += 1; continue
    total += 1; ips.add(m.group(1)); st[m.group(4)] = st.get(m.group(4), 0) + 1
    p = m.group(3).split("?")[0]; paths[p] = paths.get(p, 0) + 1; sizes.append(int(m.group(5)))
sizes.sort(); p95 = sizes[math.ceil(0.95 * len(sizes)) - 1]
top = sorted(paths.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
t0 = time.time(); subprocess.run([sys.executable, "logstats.py"], check=True, capture_output=True); dt = time.time() - t0
s = json.load(open("stats.json"))
assert s["total"] == total and s["malformed"] == mal, (s["total"], total, s["malformed"], mal)
assert {str(k): v for k, v in s["status_counts"].items()} == st
assert [list(x) for x in s["top_paths"]] == [list(x) for x in top], (s["top_paths"], top)
assert s["p95_bytes"] == p95, (s["p95_bytes"], p95)
assert s["unique_ips"] == len(ips)
assert dt < 5, f"too slow: {dt:.1f}s"
print("ok")
