"""
awc_snapshot.py — Hourly AWC economy snapshot
Runs via cron every hour to track AWC circulation, inflation, and Gini.
"""
import sqlite3, datetime, uuid, sys
sys.path.insert(0, '/root/agentworld')

DB = "/var/lib/agentworld/world.db"

def gini(arr):
    arr = sorted(arr)
    n = len(arr)
    if n == 0 or sum(arr) == 0: return 0
    cum = sum((2*(i+1) - n - 1) * v for i, v in enumerate(arr))
    return cum / (n * sum(arr))

conn = sqlite3.connect(DB, timeout=30)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA busy_timeout=20000")
c = conn.cursor()

balances = [r[0] for r in c.execute("SELECT usdc_balance FROM agents").fetchall()]
total = sum(balances)
avg = total / len(balances) if balances else 0

tick    = c.execute("SELECT COUNT(*) FROM world_events").fetchone()[0]
txns_24h = c.execute("SELECT COUNT(*) FROM transactions WHERE timestamp >= datetime('now','-24 hours')").fetchone()[0]
vol_24h  = c.execute("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE timestamp >= datetime('now','-24 hours')").fetchone()[0]
prev     = c.execute("SELECT total_awc FROM awc_snapshots ORDER BY timestamp DESC LIMIT 1").fetchone()
inflation = round(((total - prev[0]) / prev[0] * 100), 2) if prev and prev[0] else 0.0

c.execute("INSERT INTO awc_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
    str(uuid.uuid4()), tick, round(total,4), len(balances),
    round(avg,4), round(max(balances),4), round(min(balances),4),
    round(gini(balances),4), txns_24h, round(vol_24h,4), inflation,
    datetime.datetime.now(datetime.timezone.utc).isoformat()
))
conn.commit()
conn.close()
print(f"[AWC Snapshot] tick={tick} total={total:.4f} agents={len(balances)} gini={round(gini(balances),4)} inflation={inflation}%")
