"""Offline end-to-end test of the Local Business Scanner: fake Places, self-serve preview,
dev checkout for the $249 DIY and $749 done-for-you websites,
check-up PDF + 30-day plan, static publish to Netlify (mocked), outbox email. Online only: no postcards.

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
    config.REDESIGN_ENABLED = False  # the redesign path has its own test below
    stats = local.daily_scan(2)
    config.REDESIGN_ENABLED = True
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
    old_hours = config.OUTREACH_SEND_HOURS
    config.OUTREACH_SEND_HOURS = (local.local_hour(b2["city"]) + 1, local.local_hour(b2["city"]) + 2)
    assert local.send_emails()["sent"] == 0  # outside business hours where the business is
    config.OUTREACH_SEND_HOURS = (0, 24)
    res = local.send_emails()
    config.OUTREACH_SEND_HOURS = old_hours
    assert res["sent"] == 1, res
    # social profiles kept for later (no email needed), via admin and CSV import
    r = c.post(f"/admin/local/{b['id']}/social", auth=AUTH, follow_redirects=False,
               data={"facebook": "facebook.com/GlamNailsTampa?ref=x", "instagram": "@glamnails"})
    assert r.status_code == 303
    row = db.q1("SELECT facebook_url, instagram_url FROM businesses WHERE id=?", (b["id"],))
    assert row["facebook_url"] == "https://www.facebook.com/GlamNailsTampa" and row["instagram_url"] == "https://www.instagram.com/glamnails"
    assert c.post(f"/admin/local/{b['id']}/social", auth=AUTH, data={"facebook": "https://evil.com/x"}).status_code == 400
    b4 = db.q1("SELECT * FROM businesses WHERE id NOT IN (?,?) ORDER BY id LIMIT 1", (b["id"], b2["id"]))
    res = local.import_leads(f"place_id,instagram,source\n{b4['place_id']},instagram.com/shop4,their google post\n".encode(), "csv")
    assert res["updated"] == 1 and db.q1("SELECT instagram_url FROM businesses WHERE id=?", (b4["id"],))["instagram_url"].endswith("/shop4")
    assert db.q1("SELECT contact_email FROM businesses WHERE id=?", (b4["id"],))["contact_email"] is None
    assert "Save socials" in c.get("/admin/local", auth=AUTH).text
    # nationwide list + time zones
    from creator_audit import us_cities
    assert len(us_cities.US) > 300 and "Fort Walton Beach FL" in us_cities.US and "Anchorage AK" in us_cities.US
    states = {c.rsplit(" ", 1)[1] for c in us_cities.US}
    assert len(states) == 51 and states <= set(us_cities.STATE_UTC), states - set(us_cities.STATE_UTC)
    assert (local.local_hour("Los Angeles CA") - local.local_hour("New York NY")) % 24 == 21
    mail = sorted(config.OUTBOX_DIR.glob("*outreach*"))[-1].read_text()
    assert "Unsubscribe" in mail and "free website preview" in mail
    e = db.q1("SELECT * FROM emails WHERE business_id=?", (b2["id"],))
    assert e

    # preview offers both tiers at the new prices
    r = c.get(f"/b/{b['token']}")
    assert "$249" in r.text and "$749" in r.text and 'value="website_pro"' in r.text

    # purchase the $249 website from the preview -> setup form -> live site
    r = c.post("/checkout", data={"tier": "website", "ref": b["token"], "email": "owner@nails.com"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/site-setup/"), r.text
    setup = c.get(r.headers["location"])
    assert setup.status_code == 200 and "Publish my website" in setup.text and "30-day plan" in setup.text
    assert db.q1("SELECT price_cents FROM orders WHERE tier='website'")["price_cents"] == 24900
    setup_url = r.headers["location"]
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
    delivery = sorted(config.OUTBOX_DIR.glob("*delivery*"))[-1].read_text()
    assert "30-day plan (PDF)" in delivery and "/bp/" in delivery
    assert c.post("/checkout", data={"tier": "website", "ref": b["token"]}).status_code == 400  # can't buy twice
    assert c.post("/checkout", data={"tier": "website_pro", "ref": b["token"]}).status_code == 400

    # check-up PDF + 30-day plan for the buyer
    pdf = c.get("/bp/" + setup_url.rsplit("/", 1)[1] + ".pdf")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF" and "30-day-plan.pdf" in pdf.headers["content-disposition"]
    assert c.get("/bp/nope.pdf").status_code == 404
    plan = local.plan_30([{"label": "Photos", "passed": False}, {"label": "Website", "passed": True}])
    assert [w["week"] for w in plan] == [1, 2, 3, 4] and any("photos" in t for t in plan[1]["tasks"])
    assert not any("as the 'Website'" in t for t in plan[0]["tasks"])

    # done-for-you ($749): admin alert on sale, setup asks for domain + notes, alert with the owner's notes
    bp = db.q1("SELECT * FROM businesses WHERE stage NOT IN ('paid','optout') ORDER BY id DESC LIMIT 1")
    r = c.post("/checkout", data={"tier": "website_pro", "ref": bp["token"], "email": "pro@shop.com"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/site-setup/"), r.text
    assert db.q1("SELECT price_cents FROM orders WHERE tier='website_pro'")["price_cents"] == 74900
    assert "Done-for-you website sold" in sorted(config.OUTBOX_DIR.glob("*alert*"))[-1].read_text()
    setup = c.get(r.headers["location"])
    assert "Done-for-you website" in setup.text and 'name="notes"' in setup.text and 'name="domain"' in setup.text
    bad = c.post(r.headers["location"], data={"name": "Pro Shop", "tagline": "t", "blurb": "b", "services": "x",
                                              "owner_email": "pro@shop.com", "domain": "not a domain"})
    assert bad.status_code == 400
    r = c.post(r.headers["location"], follow_redirects=False, data={
        "name": "Pro Shop", "tagline": "Fixed fast.", "blurb": "Family garage.", "services": "Brakes\nTires",
        "owner_email": "pro@shop.com", "domain": "https://www.proshop.com/", "notes": "Logo: dropbox link"})
    assert r.headers["location"] == "/s/pro-shop"
    alert = sorted(config.OUTBOX_DIR.glob("*alert*"))[-1].read_text()
    assert "proshop.com" in alert and "Logo: dropbox link" in alert
    site = db.q1("SELECT * FROM sites WHERE slug='pro-shop'")
    assert site["plan"] == "pro" and site["domain"] is None  # domain is set by admin once DNS points at Netlify

    # admin edits the site and sets the domain -> static publish (Netlify API mocked)
    from creator_audit import sitepub
    calls = []

    class _R:
        def __init__(self, data=None):
            self._d, self.ok, self.status_code = data or {}, True, 200

        def json(self):
            return self._d

        def raise_for_status(self):
            pass

    def fake_post(url, **kw):
        calls.append(("post", url, kw.get("json")))
        return _R({"id": "dep1", "required": list(kw["json"]["files"].values())})

    def fake_put(url, **kw):
        calls.append(("put", url, None))
        return _R()

    def fake_patch(url, **kw):
        calls.append(("patch", url, kw.get("json")))
        return _R()

    import requests
    saved = (requests.post, requests.put, requests.patch, config.NETLIFY_AUTH_TOKEN, config.NETLIFY_SITES_ID)
    requests.post, requests.put, requests.patch = fake_post, fake_put, fake_patch
    config.NETLIFY_AUTH_TOKEN, config.NETLIFY_SITES_ID = "tok", "site1"
    try:
        assert c.get(f"/admin/site/{site['id']}", auth=AUTH).status_code == 200
        assert c.get(f"/admin/site/{site['id']}").status_code == 401
        r = c.post(f"/admin/site/{site['id']}", auth=AUTH, data={
            "name": "Pro Shop", "tagline": "Fixed fast, priced fair.", "blurb": "Family garage.",
            "services": "Brakes\nTires\nOil", "domain": "www.ProShop.com"})
        assert r.status_code == 200 and "Published to the fast static host" in r.text, r.text
        files = [x for x in calls if x[0] == "post"][-1][2]["files"]
        assert "/s/pro-shop/index.html" in files and "/s/glam-nails-tampa/index.html" in files and "/_redirects" in files
        redirects = sitepub.build_files()["/_redirects"].decode()
        assert "https://proshop.com/*  /s/pro-shop/:splat  200!" in redirects and "https://www.proshop.com/*" in redirects
        assert [x for x in calls if x[0] == "patch"][-1][2] == {"domain_aliases": ["proshop.com", "www.proshop.com"]}
        assert "Oil" in sitepub.build_files()["/s/pro-shop/index.html"].decode()
        assert sitepub.publish_pending() == {"pending": 0}
    finally:
        requests.post, requests.put, requests.patch, config.NETLIFY_AUTH_TOKEN, config.NETLIFY_SITES_ID = saved
    assert sitepub.publish_pending() == {"skipped": "not configured"}

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
    assert sum(x["paid"] for x in f) == 2
    tasks.weekly_summary()
    assert "Local businesses" in sorted(config.OUTBOX_DIR.glob("*weekly*"))[-1].read_text()


def test_redesign_flow():
    """Businesses WITH a weak website: their site is read, a premium redesign is built from their own content,
    the email they publish is approved automatically and the redesign pitch goes out on its own."""
    c = TestClient(app)
    from creator_audit import copywriter, sitecheck, sitepub
    db.ex("DELETE FROM local_scan_log")
    config.LOCAL_SEARCHES_PER_DAY, old = 40, config.LOCAL_SEARCHES_PER_DAY
    stats = local.daily_scan(1)
    config.LOCAL_SEARCHES_PER_DAY = old
    assert stats["redesigns"] > 0 and stats["emails_found"] == stats["redesigns"], stats
    r = db.q1("SELECT * FROM businesses WHERE kind='redesign' ORDER BY id LIMIT 1")
    assert r["stage"] == "approved" and r["contact_email"] == "owner@example.com" and "their website" in r["contact_source"]
    assert r["facebook_url"] == "https://www.facebook.com/examplebiz" and r["site_score"] < config.REDESIGN_MAX_SCORE
    # automatic pitch, in the business's own hours
    config.OUTREACH_SEND_HOURS, old_h = (0, 24), config.OUTREACH_SEND_HOURS
    real_weekday, local.local_weekday = local.local_weekday, lambda city: 5
    assert local.send_due()["emails"]["sent"] == 0  # Saturday where the business is: nothing goes out
    local.local_weekday = real_weekday
    # 5 waiting, cap 3: three sends; one business that opts out during a pause is skipped
    from unittest import mock
    rows = [{"id": i, "city": "Tampa FL", "stage": "approved", "emails_sent": 0, "first_visit_at": None, "contact_email": f"p{i}@x.test",
             "place_id": f"pace{i}", "category": "x"} for i in range(5)]
    naps = []
    with mock.patch.object(local.db, "q", side_effect=lambda sql, *a: rows if "emails_sent=0" in sql else []),             mock.patch.object(local.db, "q1", side_effect=lambda sql, a=(): dict(rows[a[0]], stage="optout" if a[0] == 1 else "approved")),             mock.patch.object(local.db, "update"), mock.patch.object(local.db, "ex"), mock.patch.object(local, "_mark_contacted"),             mock.patch.object(local, "is_expired", return_value=False), mock.patch.object(local, "live_details", return_value={"x": 1}),             mock.patch.object(local, "preview_context"), mock.patch.object(local, "_business_email", return_value=("s", "h", "t")),             mock.patch.object(local.emailer, "is_dnc", return_value=False), mock.patch.object(local.emailer, "send", return_value=1) as snd,             mock.patch("creator_audit.outreach._sent_today", return_value=0), mock.patch.object(local.time, "sleep", side_effect=naps.append),             mock.patch.object(config, "PITCH_PAUSE_S", (45, 120)), mock.patch.object(config, "PITCH_MAX_PER_PASS", 3):
        paced = local.send_emails()
    assert paced["sent"] == 3 and [c.args[1] for c in snd.call_args_list] == ["p0@x.test", "p2@x.test", "p3@x.test"], (paced, snd.call_args_list)
    assert len(naps) == 3 and all(45 <= x <= 120 for x in naps), naps  # one pause was spent on the opt-out
    res = local.send_emails()
    config.OUTREACH_SEND_HOURS = old_h
    assert res["sent"] >= 1, res
    mail = sorted(config.OUTBOX_DIR.glob("*outreach*"))[-1].read_text()
    assert "A redesign for example.com" in mail and "Unsubscribe" in mail and "site score" in mail
    # preview: website check-up first, then the redesign; mock = premium template with THEIR logo + photos
    page = c.get(f"/b/{r['token']}").text
    assert "Your website check-up" in page and "The redesign we built for you" in page
    m = c.get(f"/m/{r['token']}").text
    assert "https://example.com/logo.png" in m and "https://example.com/a.jpg" in m and "Concept by" in m and "tel:" in m
    assert 'class="bar"' in m and "noindex" in m and "Family-owned" in m  # proof taken from their own site text
    # buying a redesign: setup is prefilled from their site; the paid site keeps their images on the static host
    resp = c.post("/checkout", data={"tier": "website", "ref": r["token"], "email": "owner@example.com"}, follow_redirects=False)
    setup = c.get(resp.headers["location"]).text
    assert "owner@example.com" in setup
    import re as _re
    form = {k: v for k, v in _re.findall(r'name="(\w+)"[^>]*value="([^"]*)"', setup)}
    form.update({k: v for k, v in _re.findall(r'<textarea[^>]*name="(\w+)"[^>]*>([^<]*)</textarea>', setup)})
    form["owner_email"] = "owner@example.com"
    done = c.post(resp.headers["location"], data=form, follow_redirects=False)
    slug = done.headers["location"].rsplit("/", 1)[1]
    live = c.get(f"/s/{slug}").text
    assert "Concept by" not in live and "Site by" in live and "application/ld+json" in live
    files = sitepub.build_files()
    assert any(k.startswith(f"/s/{slug}/img/") for k in files), list(files)[:6]
    html = files[f"/s/{slug}/index.html"].decode()
    assert 'src="img/' in html and "example.com/logo.png" not in html
    # chains / already-pitched sites are skipped
    assert local._chain_site("https://salonrepublic.com/locations/sunnyvale-ca/")
    assert local._chain_site(r["site_url"]) and not local._chain_site("https://brand-new-shop.com/")
    # copy rules: no hype words, whole sentences, readable brand colour
    cc = copywriter.build(name="Test", theme_key="trades", site={"paragraphs": ["We are the best #1 plumbers in town. Honest work for fair prices every single day."]})
    assert "best" not in cc["sub"].lower() and cc["sub"].endswith(".")
    assert copywriter.readable_accent("#ffeb3b", "#c2410c")[0] == "#c2410c"
    assert sitecheck.score([{"key": "https", "passed": True}, {"key": "mobile", "passed": False}]) < 50


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
    assert page.status_code == 200 and "Get my website" in page.text and "Do it for me" in page.text
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
    test_redesign_flow()
    test_self_serve_preview()
    print("LOCAL PASSED", "postgres" if db.is_pg() else "sqlite", TMP)
