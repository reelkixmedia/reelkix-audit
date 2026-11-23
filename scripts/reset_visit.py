"""Reset a preview visit that was really us reviewing it. Takes a token PREFIX so the full link never hits public logs."""
import os
import sys

from creator_audit import db

prefix = os.environ.get("TOKEN", "").strip()[:10]
if len(prefix) < 8:
    sys.exit("enter at least the first 8 characters of the token")
db.init()
rows = db.q("SELECT id, stage, visits, emails_sent FROM businesses WHERE token LIKE ?", (prefix + "%",))
if len(rows) != 1:
    sys.exit(f"{len(rows)} businesses match that prefix; need exactly 1")
r = rows[0]
if r["stage"] == "visited" and not r["emails_sent"]:
    db.ex("UPDATE businesses SET stage=CASE WHEN approved_at IS NOT NULL THEN 'approved' ELSE 'built' END, visits=0, "
          "first_visit_at=NULL WHERE id=?", (r["id"],))
print(dict(db.q1("SELECT id, stage, visits FROM businesses WHERE id=?", (r["id"],))))
