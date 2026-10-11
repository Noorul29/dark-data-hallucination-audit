"""Revenue reconciliation and trust-score charts using pandas, NumPy, and Matplotlib.

Reads the SQLite database, compares the monthly revenue report with the
figures recomputed from raw orders, and flags months off by more than 3%.
"""
import os
import sqlite3
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")
CHARTS = os.path.join(HERE, "..", "dashboard")
os.makedirs(CHARTS, exist_ok=True)
TODAY = "2026-10-10"
TOLERANCE = 0.03

con = sqlite3.connect(os.path.join(DATA, "retail.db"))
actual = pd.read_sql_query("""
    SELECT substr(o.order_date, 1, 7) AS month, SUM(oi.quantity * oi.unit_price) AS actual_revenue
    FROM orders o JOIN order_items oi USING(order_id)
    WHERE o.order_date <= ? GROUP BY month""", con, params=(TODAY,))
report = pd.read_sql_query("SELECT month, reported_revenue FROM monthly_revenue_report", con)

rec = report.merge(actual, on="month")
rec["variance_pct"] = ((rec["reported_revenue"] - rec["actual_revenue"]) / rec["actual_revenue"] * 100).round(1)
rec["flagged"] = np.where(rec["variance_pct"].abs() > TOLERANCE * 100, True, False)
rec.to_csv(os.path.join(DATA, "revenue_reconciliation.csv"), index=False)
print(rec[["month", "variance_pct", "flagged"]].to_string(index=False))
print(f"Flagged {rec['flagged'].sum()} of {len(rec)} months")

NAVY, RED, GREY = "#1F3A5F", "#C0392B", "#8A8F98"

# ---- Chart 1: reported vs actual revenue ----
fig, ax = plt.subplots(figsize=(8, 4.2))
x = np.arange(len(rec))
ax.bar(x - 0.2, rec["actual_revenue"] / 1e6, 0.4, label="Actual (raw SQL)", color=NAVY)
ax.bar(x + 0.2, rec["reported_revenue"] / 1e6, 0.4, label="Reported",
       color=[RED if f else GREY for f in rec["flagged"]])
ax.set_xticks(x)
ax.set_xticklabels(rec["month"], rotation=30)
ax.set_ylabel("Revenue (millions)")
ax.set_title(f"{rec['flagged'].sum()} of {len(rec)} monthly revenue figures disagree with raw data",
             loc="left", fontweight="bold")
ax.legend(frameon=False)
ax.spines[["top", "right"]].set_visible(False)
fig.tight_layout()
fig.savefig(os.path.join(CHARTS, "revenue_reconciliation.png"), dpi=160)

# ---- Chart 2: Data Trust Scores ----
ts = pd.read_csv(os.path.join(DATA, "data_trust_scores.csv")).sort_values("trust_score")
colors = ts["trust_level"].map({"High": NAVY, "Medium": GREY, "Low": RED})
fig, ax = plt.subplots(figsize=(8, 4))
ax.barh(ts["kpi"], ts["trust_score"], color=colors)
for i, v in enumerate(ts["trust_score"]):
    ax.text(v + 1, i, f"{v}", va="center", fontsize=9)
ax.set_xlim(0, 110)
ax.set_title("Data Trust Score by KPI (0-100)", loc="left", fontweight="bold")
ax.spines[["top", "right"]].set_visible(False)
fig.tight_layout()
fig.savefig(os.path.join(CHARTS, "trust_scores.png"), dpi=160)
print("Charts saved to dashboard/")
