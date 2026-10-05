import csv, json
rows = list(csv.DictReader(open("sales.csv")))
total = round(sum(int(r["qty"]) * float(r["price"]) for r in rows), 2)
by_region, by_product = {}, {}
for r in rows:
    v = int(r["qty"]) * float(r["price"])
    by_region[r["region"]] = by_region.get(r["region"], 0) + v
    by_product[r["product"]] = by_product.get(r["product"], 0) + v
rep = json.load(open("report.json"))
assert abs(rep["total_revenue"] - total) < 0.011, (rep["total_revenue"], total)
assert {k: round(v, 2) for k, v in by_region.items()} == {k: round(v, 2) for k, v in rep["by_region"].items()}
assert rep["top_product"] == max(by_product, key=by_product.get)
print("ok")
