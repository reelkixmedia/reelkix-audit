"""Offline end-to-end test of the Local Business Scanner: fake Places, postcards written to disk,
dev checkout for the $200 website, outbox email.

    python tests/test_local.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="local_test_")
os.environ.update({"DATA_DIR": TMP, "GOOGLE_PLACES_API_KEY": "fake", "YOUTUBE_API_KEY": "fake", "ANTHROPIC_API_KEY": "",
                   "STRIPE_SECRET_KEY": "", "SMTP_HOST": "", "LOB_API_KEY": "", "ADMIN_PASSWORD": "pw",
                   "BASE_URL": "http://testserver", "POSTCARD_MONTHLY_BUDGET_CENTS": "900", "POSTCARD_COST_CENTS": "90",
                   "LOCAL_CATEGORIES": "nail salon,auto repair", "LOCAL_CITIES": "Tampa FL", "ADMIN_EMAIL": "o@example.com"})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from creator_audit import config, db, local, tasks  # noqa: E402
from creator_audit.app import app  # noqa: E402

AUTH = ("admin", "pw")


def test_local_flow():
    db.init()
    c = TestClient(app)

    # scan: keeps only businesses without a website, stores only place IDs + our own audit
    stats = local.daily_scan(2)
    assert stats["searches"] == 2 and stats["kept"] > 0 and stats["kept"] == stats["no_website"], stats
    cols = {r[1] for r in db.conn().execute("PRAGMA table_info(businesses)")}
    assert not cols & {"name", "address", "phone", "rating", "lat", "lng"}, "only place IDs may be stored"
    b = db.q1("SELECT * FROM businesses ORDER BY id LIMIT 1")
    assert json.loads(b["checks_json"])[0]["key"] == "website"

    # preview + mock site
    r = c.get(f"/b/{b['token']}")
    assert r.status_code == 200 and "listing check-up" in r.text and "No website is listed" in r.text
    assert r.headers["x-robots-tag"].startswith("noindex")
    m = c.get(f"/m/{b['token']}")
    assert "not affiliated with" in m.text and "noindex" in m.text and "<form" not in m.text, "mock must not take orders"
    assert "tel:" in m.text
    assert c.get(f"/qr/{b['token']}.png").headers["content-type"] == "image/png"
    assert db.q1("SELECT stage, visits FROM businesses WHERE id=?", (b["id"],))["visits"] == 1

    # postcards: automatic within budget (900c / 90c = 10)
    res = local.send_postcards()
    assert res["sent"] == min(10, stats["kept"] - 0) or res.get("budget_reached"), res
    cards = list(config.POSTCARD_DIR.glob("*.html"))
    assert cards and "not affiliated" in cards[0].read_text(encoding="utf-8")
    assert "/optout/" in cards[0].read_text(encoding="utf-8")
    assert local.send_postcards()["sent"] == 0  # budget used / nothing new

    # email channel: person-found email -> automatic send with CAN-SPAM footer
    b2 = db.q1("SELECT * FROM businesses WHERE id!=? ORDER BY id LIMIT 1", (b["id"],))
    r = c.post(f"/admin/local/{b2['id']}/contact", auth=AUTH, follow_redirects=False,
               data={"email": "owner@salon.com", "source": "https://facebook.com/salon", "approve": "1"})
    assert r.status_code == 303
    res = local.send_emails()
    assert res["sent"] == 1, res
    mail = [p for p in config.OUTBOX_DIR.glob("*outreach*")][-1].read_text()
    assert "Unsubscribe" in mail and "free website preview" in mail
    e = db.q1("SELECT * FROM emails WHERE business_id=?", (b2["id"],))
    assert e

    # purchase the $200 website from the preview -> setup form -> live site
    r = c.post("/checkout", data={"tier": "website", "ref": b["token"], "email": "owner@nails.com"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/site-setup/"), r.text
    setup = c.get(r.headers["location"])
    assert setup.status_code == 200 and "Publish my website" in setup.text
    r = c.post(r.headers["location"], follow_redirects=False, data={
        "name": "Glam Nails Tampa", "tagline": "Nails done right.", "blurb": "Friendly studio.",
        "services": "Manicure\nPedicure\nGel", "phone": "(813) 555-1234", "email": "", "address": "1 Main St",
        "city": "Tampa", "maps_uri": "https://maps.google.com/?cid=1", "hours": "Monday: 9 AM – 6 PM",
        "owner_email": "owner@nails.com"})
    assert r.headers["location"] == "/s/glam-nails-tampa"
    live = c.get("/s/glam-nails-tampa")
    assert live.status_code == 200 and "Manicure" in live.text and "not affiliated" not in live.text and "noindex" not in live.text
    bb = db.q1("SELECT * FROM businesses WHERE id=?", (b["id"],))
    assert bb["stage"] == "paid" and bb["expires_at"] is None
    assert any("delivery" in p.name for p in config.OUTBOX_DIR.glob("*.eml"))
    assert c.post("/checkout", data={"tier": "website", "ref": b["token"]}).status_code == 400  # can't buy twice

    # opt-out -> DNC -> no more mail; preview gone
    b3 = db.q1("SELECT * FROM businesses WHERE stage NOT IN ('paid') AND postcard_sent_at IS NULL LIMIT 1") or \
        db.q1("SELECT * FROM businesses WHERE stage NOT IN ('paid') LIMIT 1")
    c.post(f"/optout/{b3['token']}")
    assert db.q1("SELECT stage FROM businesses WHERE id=?", (b3["id"],))["stage"] == "optout"
    assert db.q1("SELECT 1 FROM dnc WHERE channel_id=?", ("place:" + b3["place_id"],))
    assert c.get(f"/b/{b3['token']}").status_code == 410
    assert local.daily_scan(1)["kept"] >= 0  # rescans skip DNC + known places

    # email unsubscribe for a business
    c.get(f"/u/{e['token']}")
    assert db.q1("SELECT stage FROM businesses WHERE id=?", (b2["id"],))["stage"] == "optout"

    # expiry
    db.ex("UPDATE businesses SET expires_at='2000-01-01T00:00:00+00:00' WHERE stage IN ('built','contacted','visited')")
    local.expire_and_purge()
    assert db.q1("SELECT COUNT(*) n FROM businesses WHERE stage='expired'")["n"] > 0

    # admin + funnel + weekly summary
    assert c.get("/admin/local", auth=AUTH).status_code == 200
    f = local.funnel()
    assert f and f[0]["scanned"] > 0
    assert sum(x["paid"] for x in f) == 1
    tasks.weekly_summary()
    assert "Local businesses" in [p for p in config.OUTBOX_DIR.glob("*weekly*")][-1].read_text()


if __name__ == "__main__":
    test_local_flow()
    print("LOCAL PASSED", TMP)
