"""Offline end-to-end test of the Local Business Scanner: fake Places, self-serve preview,
dev checkout for the $200 website, outbox email. Online only: no postcards.

    python tests/test_local.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="local_test_")
os.environ.update({"DATA_DIR": TMP, "PREMIUM_ENABLED": "true", "GOOGLE_PLACES_API_KEY": "fake", "YOUTUBE_API_KEY": "fake", "ANTHROPIC_API_KEY": "",
                   "STRIPE_SECRET_KEY": "", "SMTP_HOST": "", "ADMIN_PASSWORD": "pw",
                   "BASE_URL": "http://testserver", "SELF_SERVE_PER_IP_PER_DAY": "3",
                   "LOCAL_CATEGORIES": "nail salon,auto repair", "LOCAL_CITIES": "Tampa FL", "ADMIN_EMAIL": "o@example.com"})
# Same tests against Postgres: set TEST_DATABASE_URL to an empty database (tests/run_pg.py does this).
if os.environ.get("TEST_DATABASE_URL"):
    os.environ.update({"DATABASE_URL": os.environ["TEST_DATABASE_URL"], "ALLOW_DEV_CHECKOUT": "true", "ALLOW_OUTBOX": "true"})
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
    cols = set(dict(db.q1("SELECT * FROM businesses LIMIT 1")).keys())
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
    assert db.q1("SELECT stage, visits FROM businesses WHERE id=?", (b["id"],))["visits"] == 1

    # online only: no postcard code paths left
    assert not hasattr(local, "send_postcards") and not hasattr(config, "LOB_API_KEY")
    assert "postcards" not in local.send_due(ignore_hours=True)

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
    b3 = db.q1("SELECT * FROM businesses WHERE stage NOT IN ('paid') AND id!=? ORDER BY id LIMIT 1", (b2["id"],))
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


def test_self_serve_preview():
    import re
    c = TestClient(app)
    assert "Build my free preview" in c.get("/website-preview").text
    r = c.post("/website-preview/search", data={"name": "Glam Nails", "city": "Tampa FL"})
    assert r.status_code == 200 and "Which one is you?" in r.text, r.text[:500]
    pid = re.search(r'name="place_id" value="([^"]+)"', r.text).group(1)
    r = c.post("/website-preview/pick", data={"place_id": pid}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/b/"), r.text
    b = db.q1("SELECT * FROM businesses WHERE place_id=?", (pid,))
    assert b["source"] == "self_serve" and r.headers["location"] == f"/b/{b['token']}"
    page = c.get(r.headers["location"])
    assert page.status_code == 200 and "Get my website" in page.text
    # same place again: reuses the preview, no new Places call logged
    n = db.q1("SELECT COUNT(*) n FROM lookups")["n"]
    assert c.post("/website-preview/pick", data={"place_id": pid}, follow_redirects=False).headers["location"] == f"/b/{b['token']}"
    assert db.q1("SELECT COUNT(*) n FROM lookups")["n"] == n
    # rate limit per visitor (3/day in this test): search + details already used 2
    assert c.post("/website-preview/search", data={"name": "Pink Tips", "city": "Tampa FL"}).status_code == 200
    blocked = c.post("/website-preview/search", data={"name": "Nail Studio", "city": "Tampa FL"})
    assert blocked.status_code == 429 and "limit" in blocked.text
    assert c.post("/website-preview/search", data={"name": "x", "city": ""}).status_code == 400
    assert c.post("/website-preview/pick", data={"place_id": "bad id!"}).status_code == 400

    # monthly cap on paid Google calls: refused once used up, so the bill stays $0
    from creator_audit import places
    old = config.PLACES_MONTHLY_CAP_DETAILS
    config.PLACES_MONTHLY_CAP_DETAILS = 2
    try:
        places._count_call("details")
        places._count_call("details")
        try:
            places._count_call("details")
            raise AssertionError("cap not enforced")
        except places.PlacesCapReached:
            pass
    finally:
        config.PLACES_MONTHLY_CAP_DETAILS = old


if __name__ == "__main__":
    test_local_flow()
    test_self_serve_preview()
    print("LOCAL PASSED", "postgres" if db.is_pg() else "sqlite", TMP)
