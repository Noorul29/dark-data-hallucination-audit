"""Compute a Data Trust Score (0-100) for each KPI.

Score = average of three dimensions:
  Completeness: share of records that are present and usable
  Consistency:  share of records that agree with the source of truth or the rules
  Freshness:    1 - (days since last update / 30), floored at 0
Output: data/data_trust_scores.csv (ready for Power BI)
"""
import os, json, csv, sqlite3, glob
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(HERE, "..", "data")
DARK = os.path.join(BASE, "dark_data")
TODAY = date(2026, 10, 10)
FRESH_WINDOW_DAYS = 30
con = sqlite3.connect(os.path.join(BASE, "retail.db"))
q = lambda sql, *a: con.execute(sql, a).fetchall()


def freshness(last_update):
    lag = (TODAY - last_update).days
    return max(0.0, 1 - lag / FRESH_WINDOW_DAYS), lag


def ratio(good, total):
    return good / total if total else 0.0


scores = []

# 1. Monthly revenue report (reporting layer vs raw transactions)
true_rev = dict(q("""SELECT substr(o.order_date,1,7), SUM(oi.quantity*oi.unit_price)
                     FROM orders o JOIN order_items oi USING(order_id)
                     WHERE o.order_date <= ? GROUP BY 1""", str(TODAY)))
rep = q("SELECT month, reported_revenue FROM monthly_revenue_report")
comp = ratio(sum(1 for m, r in rep if r is not None and m in true_rev), len(rep))
cons = ratio(sum(1 for m, r in rep if abs(r - true_rev[m]) / true_rev[m] <= 0.03), len(rep))
fresh, lag = freshness(date.fromisoformat(q("SELECT MAX(generated_on) FROM monthly_revenue_report")[0][0]))
scores.append(("Monthly Revenue Report", comp, cons, fresh, lag))

# 2. Order transactions (primary database)
total = q("SELECT COUNT(*) FROM orders")[0][0]
valid_dates = q("SELECT COUNT(*) FROM orders WHERE order_date IS NOT NULL AND order_date <= ?", str(TODAY))[0][0]
orphans = q("""SELECT COUNT(*) FROM order_items oi
               LEFT JOIN orders o USING(order_id) WHERE o.order_id IS NULL""")[0][0]
items = q("SELECT COUNT(*) FROM order_items")[0][0]
last_order = date.fromisoformat(q("SELECT MAX(order_date) FROM orders WHERE order_date <= ?", str(TODAY))[0][0])
fresh, lag = freshness(last_order)
scores.append(("Order Transactions", ratio(valid_dates, total), ratio(items - orphans, items), fresh, lag))

# 3. Inventory stock levels
inv_total = q("SELECT COUNT(*) FROM inventory")[0][0]
non_null = q("SELECT COUNT(*) FROM inventory WHERE stock_on_hand IS NOT NULL")[0][0]
non_neg = q("SELECT COUNT(*) FROM inventory WHERE stock_on_hand >= 0")[0][0]
fresh, lag = freshness(date.fromisoformat(q("SELECT MAX(last_updated) FROM inventory")[0][0]))
scores.append(("Inventory Stock Levels", ratio(non_null, inv_total), ratio(non_neg, inv_total), fresh, lag))

# 4. Inventory forecast (model output vs actual sales)
actual = dict(q("""SELECT product_id, SUM(quantity) FROM order_items JOIN orders USING(order_id)
                   WHERE order_date <= ? GROUP BY product_id""", str(TODAY)))
fc = q("SELECT product_id, forecast_units, generated_on FROM inventory_forecast")
ok = sum(1 for p, f, _ in fc if f is not None and (actual.get(p, 0) / 9 == 0 or f / (actual.get(p, 0) / 9) <= 2))
fresh, lag = freshness(date.fromisoformat(max(g for _, _, g in fc)))
scores.append(("Inventory Forecast", ratio(sum(1 for _, f, _ in fc if f is not None), len(fc)),
               ratio(ok, len(fc)), fresh, lag))

# 5. Clickstream logs (dark data)
files = sorted(glob.glob(os.path.join(DARK, "clickstream", "*.jsonl")))
rows = nulls = consistent_files = 0
latest = date(2000, 1, 1)
schemas = []
for f in files:
    recs = [json.loads(l) for l in open(f)]
    keyset = frozenset(k for r in recs for k in r)
    schemas.append(keyset)
    idk = "user_id" if "user_id" in keyset else "userId"
    rows += len(recs)
    nulls += sum(r.get(idk) is None for r in recs)
    latest = max(latest, max(date.fromisoformat(r["ts"][:10]) for r in recs))
dominant = max(set(schemas), key=schemas.count)
consistent_files = sum(1 for s in schemas if s == dominant)
fresh, lag = freshness(latest)
scores.append(("Clickstream Logs (dark data)", ratio(rows - nulls, rows), ratio(consistent_files, len(files)), fresh, lag))

# ---- Output ----
def label(s):
    return "High" if s >= 90 else "Medium" if s >= 70 else "Low"

out = []
for name, c, k, f, lag in scores:
    total_score = round((c + k + f) / 3 * 100, 1)
    out.append({"kpi": name, "completeness": round(c * 100, 1), "consistency": round(k * 100, 1),
                "freshness": round(f * 100, 1), "days_since_update": lag,
                "trust_score": total_score, "trust_level": label(total_score)})

with open(os.path.join(BASE, "data_trust_scores.csv"), "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(out[0]))
    w.writeheader()
    w.writerows(out)

print(f"{'KPI':32s}{'Comp':>7s}{'Cons':>7s}{'Fresh':>7s}{'Score':>8s}  Level")
for r in out:
    print(f"{r['kpi']:32s}{r['completeness']:7.1f}{r['consistency']:7.1f}{r['freshness']:7.1f}{r['trust_score']:8.1f}  {r['trust_level']}")
