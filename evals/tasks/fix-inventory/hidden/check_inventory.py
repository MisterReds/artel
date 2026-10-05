import os, sys
sys.path.insert(0, os.getcwd())
from inventory import Inventory, OutOfStock
inv = Inventory(); inv.add("a", 2, 10.0)
try:
    inv.remove("a", 3); raise AssertionError("should raise OutOfStock")
except OutOfStock:
    pass
inv.remove("a", 2); assert inv.items["a"][0] == 0
inv.add("b", 1, 5.0); inv.add("c", 3, 2.0)
assert abs(inv.total_value() - 11.0) < 1e-9, inv.total_value()
try:
    inv.remove("zz", 1); raise AssertionError("unknown sku must raise KeyError")
except KeyError:
    pass
print("ok")
