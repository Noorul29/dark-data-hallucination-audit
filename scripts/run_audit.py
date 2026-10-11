"""Run the Dark Data & Data Hallucination audit.

Phase 1: catalog and profile unindexed (dark) files.
Phase 2: validate the reporting layer and database against raw records.
Then compare what was detected with data/answer_key.json.
"""
import os, json, csv, sqlite3, glob
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(HERE, "..", "data")
DARK = os.path.join(BASE, "dark_data")
TODAY = "2026-10-10"
REVENUE_TOLERANCE = 0.03    # flag months off by more than 3%
FORECAST_RATIO = 2.0        # flag forecasts above 2x actual monthly average
NULL_THRESHOLD = 0.20       # flag files with more than 20% missing ids

con = sqlite3.connect(os.path.join(BASE, "retail.db"))
found = {}

# ---------------- Phase 1: dark data discovery & profiling ----------------
catalog = []
key_sets = {}
null_rate = {}
for path in sorted(glob.glob(os.path.join(DARK, "**", "*.*"), recursive=True)):
    name, ext = os.path.basename(path), os.path.splitext(path)[1]
    rows = 0
    if ext == ".jsonl":
        recs = [json.loads(l) for l in open(path)]
        rows = len(recs)
        keys = frozenset(k for r in recs for k in r)
        key_sets[name] = keys
        idk = "user_id" if "user_id" in keys else "userId"
        null_rate[name] = sum(r.get(idk) is None for r in recs) / rows
    elif ext == ".json":
        rows = len(json.load(open(path)))
    elif ext == ".csv":
        rows = sum(1 for _ in open(path)) - 1
    catalog.append({"file": name, "type": ext.lstrip("."), "rows": rows,
                    "size_kb": round(os.path.getsize(path) / 1024, 1)})

with open(os.path.join(BASE, "dark_data_catalog.csv"), "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["file", "type", "rows", "size_kb"])
    w.writeheader()
    w.writerows(catalog)

common = Counter(key_sets.values()).most_common(1)[0][0]
found["schema_drift_files"] = sorted(n for n, k in key_sets.items() if k != common)
found["high_null_files"] = sorted(n for n, r in null_rate.items() if r > NULL_THRESHOLD)

# ---------------- Phase 2: validation & reconciliation ----------------
found["future_timestamps"] = sorted(r[0] for r in con.execute(
    "SELECT order_id FROM orders WHERE order_date > ?", (TODAY,)))
found["negative_inventory"] = sorted(r[0] for r in con.execute(
    "SELECT product_id FROM inventory WHERE stock_on_hand < 0"))

true_rev = dict(con.execute("""
    SELECT substr(o.order_date,1,7), SUM(oi.quantity*oi.unit_price)
    FROM orders o JOIN order_items oi USING(order_id)
    WHERE o.order_date <= ? GROUP BY 1""", (TODAY,)).fetchall())
mismatch = []
for month, reported in con.execute("SELECT month, reported_revenue FROM monthly_revenue_report"):
    diff = (reported - true_rev[month]) / true_rev[month]
    if abs(diff) > REVENUE_TOLERANCE:
        mismatch.append((month, round(diff * 100, 1)))
found["report_mismatch_months"] = sorted(m for m, _ in mismatch)

actual = dict(con.execute("""
    SELECT product_id, SUM(quantity) FROM order_items
    JOIN orders USING(order_id) WHERE order_date <= ? GROUP BY product_id""", (TODAY,)).fetchall())
over = []
for pid, fc in con.execute("SELECT product_id, forecast_units FROM inventory_forecast"):
    avg = actual.get(pid, 0) / 9
    if avg > 0 and fc / avg > FORECAST_RATIO:
        over.append(pid)
found["overforecast_products"] = sorted(over)

# ---------------- Score detection against the answer key ----------------
key = json.load(open(os.path.join(BASE, "answer_key.json")))
planted_total = detected_total = false_pos_total = 0
lines = ["DATA HEALTH & RECONCILIATION LOG", "=" * 34]
for issue, planted in key.items():
    if issue == "interpolated_month":
        planted = [planted]
        got = [m for m in found["report_mismatch_months"] if m == planted[0]]
        fp = 0
        planted_total += 1
        detected_total += len(got)
        lines.append(f"{issue:26s} {len(got)}/1 detected (flagged by revenue check: {'yes' if got else 'no'})")
        continue
    expected = set(planted)
    if issue == "report_mismatch_months" and key["interpolated_month"]:
        expected.add(key["interpolated_month"])  # revenue check should also flag the interpolated month
    got = set(found[issue]) & set(planted)
    fp = len(set(found[issue]) - expected)
    planted_total += len(planted)
    detected_total += len(got)
    false_pos_total += fp
    lines.append(f"{issue:26s} {len(got)}/{len(planted)} detected, {fp} false positive(s)")

lines += ["-" * 34,
          f"TOTAL planted issues: {planted_total}",
          f"TOTAL detected:       {detected_total}",
          f"TOTAL false positives: {false_pos_total}",
          f"Detection rate:       {detected_total / planted_total:.1%}",
          "", "Revenue months off by more than 3%: " + ", ".join(f"{m} ({d:+}%)" for m, d in mismatch),
          f"Dark files cataloged: {len(catalog)} | rows profiled: {sum(c['rows'] for c in catalog):,}"]
report = "\n".join(lines)
print(report)
open(os.path.join(BASE, "audit_log.txt"), "w").write(report + "\n")
