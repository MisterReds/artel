import re
t = open("adr-001.md").read()
for h in ["Context", "Decision", "Alternatives", "Consequences"]:
    assert re.search(rf"^#+\s*{h}", t, re.M | re.I), f"missing section {h}"
low = t.lower()
for w in ["sqlite", "postgres", "backup", "migration"]:
    assert w in low or (w == "backup" and "бэкап" in low) or (w == "migration" and "миграц" in low), f"missing {w}"
assert "wal" in low or "concurren" in low or "конкурент" in low, "concurrency not addressed"
n = len(re.findall(r"\w+", t))
assert 300 <= n <= 900, f"length {n}"
print("ok")
