"""Generate simulated retail data for the Dark Data & Data Hallucination Audit.
All data is synthetic. Known problems are injected on purpose and logged to
data/answer_key.json so you can measure how many your audit scripts catch."""
import sqlite3, json, csv, random, os
from datetime import datetime, timedelta

random.seed(42)
BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
DARK = os.path.join(BASE, "dark_data")
for d in ("clickstream", "abandoned_carts", "support_chats"):
    os.makedirs(os.path.join(DARK, d), exist_ok=True)

DB = os.path.join(BASE, "retail.db")
if os.path.exists(DB):
    os.remove(DB)
con = sqlite3.connect(DB)
cur = con.cursor()
cur.executescript("""
CREATE TABLE customers(customer_id INTEGER PRIMARY KEY, name TEXT, city TEXT, signup_date TEXT);
CREATE TABLE products(product_id INTEGER PRIMARY KEY, name TEXT, category TEXT, unit_price REAL);
CREATE TABLE orders(order_id INTEGER PRIMARY KEY, customer_id INTEGER, order_date TEXT, channel TEXT);
CREATE TABLE order_items(item_id INTEGER PRIMARY KEY, order_id INTEGER, product_id INTEGER, quantity INTEGER, unit_price REAL);
CREATE TABLE inventory(product_id INTEGER PRIMARY KEY, stock_on_hand INTEGER, last_updated TEXT);
CREATE TABLE monthly_revenue_report(month TEXT PRIMARY KEY, reported_revenue REAL, generated_on TEXT);
CREATE TABLE inventory_forecast(product_id INTEGER PRIMARY KEY, forecast_units INTEGER, generated_on TEXT);
""")

cities = ["Chennai", "Mumbai", "Delhi", "Pune", "Hyderabad", "Kolkata", "Bengaluru"]
cats = ["Electronics", "Fashion", "Home", "Grocery", "Beauty", "Sports"]
channels = ["web", "store"]
START, END = datetime(2026, 1, 1), datetime(2026, 9, 30)
TODAY = "2026-10-10"
key = {"future_timestamps": [], "negative_inventory": [], "report_mismatch_months": [],
       "interpolated_month": None, "overforecast_products": [], "schema_drift_files": [],
       "high_null_files": []}

for i in range(1, 501):
    cur.execute("INSERT INTO customers VALUES(?,?,?,?)",
                (i, f"Customer {i}", random.choice(cities),
                 (START - timedelta(days=random.randint(1, 700))).strftime("%Y-%m-%d")))
for i in range(1, 61):
    cur.execute("INSERT INTO products VALUES(?,?,?,?)",
                (i, f"Product {i}", random.choice(cats), round(random.uniform(99, 4999), 2)))
prices = dict(cur.execute("SELECT product_id, unit_price FROM products").fetchall())

item_id = 1
for oid in range(1, 3001):
    d = START + timedelta(days=random.randint(0, (END - START).days))
    cur.execute("INSERT INTO orders VALUES(?,?,?,?)",
                (oid, random.randint(1, 500), d.strftime("%Y-%m-%d"), random.choice(channels)))
    for _ in range(random.randint(1, 4)):
        pid = random.randint(1, 60)
        cur.execute("INSERT INTO order_items VALUES(?,?,?,?,?)",
                    (item_id, oid, pid, random.randint(1, 5), prices[pid]))
        item_id += 1

# Revenue report is built from clean data first, so the reporting layer starts "correct"
rows = cur.execute("""SELECT substr(o.order_date,1,7) m, SUM(oi.quantity*oi.unit_price)
                      FROM orders o JOIN order_items oi USING(order_id)
                      GROUP BY m ORDER BY m""").fetchall()
true_rev = {m: r for m, r in rows}
months = sorted(true_rev)
wrong = {months[1]: 1.08, months[4]: 0.88, months[6]: 1.25}
interp_month = months[3]
for i, m in enumerate(months):
    rep = true_rev[m]
    if m in wrong:
        rep *= wrong[m]
        key["report_mismatch_months"].append(m)
    if m == interp_month:
        rep = (true_rev[months[i - 1]] + true_rev[months[i + 1]]) / 2
        key["interpolated_month"] = m
    cur.execute("INSERT INTO monthly_revenue_report VALUES(?,?,?)", (m, round(rep, 2), TODAY))

# Forecast: based on average monthly units, but some products overforecast 3x
units = dict(cur.execute("SELECT product_id, SUM(quantity) FROM order_items GROUP BY product_id").fetchall())
bad = random.sample(range(1, 61), 8)
for pid in range(1, 61):
    f = int(units.get(pid, 0) / 9)
    if pid in bad:
        f *= 3
        key["overforecast_products"].append(pid)
    cur.execute("INSERT INTO inventory_forecast VALUES(?,?,?)", (pid, f, TODAY))

# Inventory with some impossible negative values
for pid in range(1, 61):
    cur.execute("INSERT INTO inventory VALUES(?,?,?)", (pid, random.randint(20, 400), TODAY))
for pid in random.sample(range(1, 61), 6):
    cur.execute("UPDATE inventory SET stock_on_hand=? WHERE product_id=?", (-random.randint(5, 60), pid))
    key["negative_inventory"].append(pid)

# Inject future timestamps into transactions (after the report was built)
for oid in random.sample(range(1, 3001), 12):
    cur.execute("UPDATE orders SET order_date=? WHERE order_id=?",
                ("2027-0%d-15" % random.randint(1, 9), oid))
    key["future_timestamps"].append(oid)
con.commit()
con.close()

# ---- Dark data (unindexed files with no metadata) ----
pages = ["home", "category", "product", "cart", "checkout"]
for n in range(30):
    day = START + timedelta(days=n * 8)
    fn = f"clicks_{day.strftime('%Y%m%d')}.jsonl"
    drift = n % 7 == 0   # schema drift: userId instead of user_id
    nulls = n % 10 == 3  # many missing user ids
    with open(os.path.join(DARK, "clickstream", fn), "w") as f:
        for _ in range(random.randint(150, 300)):
            uid = random.randint(1, 500)
            rec = {("userId" if drift else "user_id"): (None if nulls and random.random() < 0.6 else uid),
                   "page": random.choice(pages),
                   "ts": (day + timedelta(seconds=random.randint(0, 86399))).isoformat()}
            f.write(json.dumps(rec) + "\n")
    if drift:
        key["schema_drift_files"].append(fn)
    if nulls:
        key["high_null_files"].append(fn)

for n in range(12):
    fn = f"cart_{n + 1:03d}.json"
    carts = [{"customer_id": random.randint(1, 500),
              "items": [{"product_id": random.randint(1, 60), "qty": random.randint(1, 3)}
                        for _ in range(random.randint(1, 4))],
              "abandoned_at": (START + timedelta(days=random.randint(0, 270))).isoformat()}
             for _ in range(random.randint(40, 90))]
    with open(os.path.join(DARK, "abandoned_carts", fn), "w") as f:
        json.dump(carts, f)

issues = ["late delivery", "refund request", "wrong item", "app crash", "price mismatch", "cancel order"]
for n in range(6):
    with open(os.path.join(DARK, "support_chats", f"chat_dump_{n + 1}.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["customer_id", "issue", "message", "date"])
        for _ in range(random.randint(80, 150)):
            iss = random.choice(issues)
            w.writerow([random.randint(1, 500), iss, f"Customer reported: {iss}",
                        (START + timedelta(days=random.randint(0, 270))).strftime("%Y-%m-%d")])

with open(os.path.join(BASE, "answer_key.json"), "w") as f:
    json.dump(key, f, indent=2)
print("Done.")
