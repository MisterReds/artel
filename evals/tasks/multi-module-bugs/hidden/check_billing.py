import os, sys
from datetime import datetime, timezone, date
from decimal import Decimal as D
sys.path.insert(0, os.getcwd())
from billing import Invoice, Line
from billing.money import round_money
assert round_money(D("2.675")) == D("2.68"), round_money(D("2.675"))   # half-up, no float drift
assert round_money(D("1.005")) == D("1.01")
inv = Invoice(datetime(2026, 3, 1, 22, 30, tzinfo=timezone.utc),
              [Line("a", 3, D("10.10"), 20), Line("b", 1, D("99.99"), 10), Line("c", 2, D("5.00"), 0)])
assert inv.subtotal() == D("140.29"), inv.subtotal()
assert inv.vat_total() == D("16.06"), inv.vat_total()      # 30.30*20% + 99.99*10% (per-line, rounded)
assert inv.total() == D("156.35"), inv.total()
assert inv.due() == date(2026, 3, 12), inv.due()           # 22:30 UTC is already 2 March in Moscow
assert Invoice(datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc), [], 10).due() == date(2026, 3, 11)
print("ok")
