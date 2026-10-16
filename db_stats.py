import sqlite3
con = sqlite3.connect("job_monitor.db")
cur = con.cursor()
for (t,) in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
    cols = [c[1] for c in cur.execute(f"PRAGMA table_info({t})")]
    n = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"\n{t}: {n} rows | columns: {cols}")
    for c in ("source", "notified", "flagged", "spam_flag"):
        if c in cols:
            print(f"  by {c}:", cur.execute(f"SELECT {c}, COUNT(*) FROM {t} GROUP BY {c}").fetchall())
    for c in ("first_seen", "created_at", "seen_at"):
        if c in cols:
            print(f"  date range ({c}):", cur.execute(f"SELECT MIN({c}), MAX({c}) FROM {t}").fetchone())