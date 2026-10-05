import os, sys, threading
sys.path.insert(0, os.getcwd())
from ttlcache import TTLCache
t = [0.0]
c = TTLCache(maxsize=2, ttl=10, clock=lambda: t[0])
c.set("a", 1); c.set("b", 2); assert c.get("a") == 1
c.set("c", 3)                      # evicts b (LRU), not a
assert "b" not in c and c.get("a") == 1 and c.get("c") == 3
assert c.stats["evictions"] == 1, c.stats
t[0] = 11
assert c.get("a") is None and c.get("a", "d") == "d"
assert c.stats["expirations"] >= 1 and len(c) == 0, (c.stats, len(c))
exp = c.stats["expirations"]; c.get("a"); assert c.stats["expirations"] == exp  # counted once
c.set("x", 1); assert c.delete("x") is True and c.delete("x") is False
s = c.stats; assert s["hits"] >= 3 and s["misses"] >= 2, s
big = TTLCache(maxsize=100, ttl=1000)
def worker(k):
    for i in range(2000):
        big.set((k, i % 150), i); big.get((k, (i * 7) % 150))
ts = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
[x.start() for x in ts]; [x.join() for x in ts]
assert len(big) <= 100
print("ok")
