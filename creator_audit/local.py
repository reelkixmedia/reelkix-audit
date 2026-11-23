"""Local Business Scanner: Google Places -> no-website businesses -> mini audit + mock homepage
-> email (contact found by a person or lead source) or the self-serve preview page (the business
looks itself up) -> preview visit -> website purchase ($249 DIY or $749 done-for-you, or consult) -> owner fills setup form
-> site goes live (static copy on Netlify) + check-up PDF with a 30-day plan.

Online only (owner decision 2026-11-16): no postcards, no texts, no calls.

Guardrails from the spec: only place IDs are stored (details are re-fetched), no Google photos on
mock sites, "Concept by Reelkix, not affiliated" banner, noindex + private link, expiry, no
booking/ordering/payment forms acting as the business, no automated texts, no calls.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import logging
import re
import time
from datetime import timedelta

from . import config, copywriter, db, emailer, places, sitecheck

log = logging.getLogger("local")

# ------------------------------------------------------------------ audit
CHECKS = [
    # key, weight, label
    ("website", 30, "Website"),
    ("hours", 15, "Opening hours"),
    ("photos", 15, "Photos"),
    ("rating", 15, "Rating"),
    ("reviews", 15, "Review count"),
    ("phone", 5, "Phone number"),
    ("ai", 5, "Named by AI assistants"),
]
PHOTO_TARGET = 10  # Places returns at most 10 photo references
RATING_TARGET = 4.3
REVIEW_TARGET = 25


def run_checks(d: dict, ai_mentioned: bool | None) -> tuple[int, list[dict]]:
    passed = {
        "website": bool(d.get("website")),
        "hours": bool(d.get("hours")),
        "photos": d.get("photos", 0) >= PHOTO_TARGET,
        "rating": (d.get("rating") or 0) >= RATING_TARGET,
        "reviews": (d.get("reviews") or 0) >= REVIEW_TARGET,
        "phone": bool(d.get("phone")),
        "ai": ai_mentioned,
    }
    checks, got, total = [], 0, 0
    for key, w, _ in CHECKS:
        if passed[key] is None:
            continue
        total += w
        got += w if passed[key] else 0
        checks.append({"key": key, "passed": bool(passed[key])})
    return (round(got / total * 100) if total else 0), checks


def insights(d: dict, checks: list[dict], category: str, city: str) -> list[dict]:
    """Human-readable findings. Values come from the live Place Details fetch, so nothing
    from Google is stored; the text is fixed templates (no AI-written numbers)."""
    name = d.get("name") or "Your business"
    out = []
    for c in checks:
        k, ok = c["key"], c["passed"]
        if k == "website":
            out.append({"label": "Website", "passed": ok,
                        "finding": "You have a website linked." if ok else
                        f"No website is listed on your Google profile. People searching for a {category} in {city} can't click through to see your services, prices or hours.",
                        "fix": "" if ok else "Add a simple website with your services, hours, a call button and a map, and link it on your Google Business Profile. (We built you one below.)"})
        elif k == "hours":
            out.append({"label": "Opening hours", "passed": ok,
                        "finding": "Your opening hours are listed." if ok else "Your opening hours are missing, so Google can't show you as 'Open now' and customers aren't sure when to come.",
                        "fix": "" if ok else "Add your regular hours (and holiday hours) in your Google Business Profile."})
        elif k == "photos":
            n = d.get("photos", 0)
            out.append({"label": "Photos", "passed": ok,
                        "finding": f"Your profile has {n} photo{'s' if n != 1 else ''}." + ("" if ok else " Profiles with plenty of recent photos look more active and trustworthy."),
                        "fix": "" if ok else f"Upload at least {PHOTO_TARGET} clear photos: storefront, inside, your work, your team. Add a few new ones every month."})
        elif k == "rating":
            r = d.get("rating")
            out.append({"label": "Rating", "passed": ok,
                        "finding": f"Your rating is {r} stars." if r else "You don't have a star rating yet.",
                        "fix": "" if ok else "Reply to every review (good and bad), fix what complaints point to, and ask happy customers to leave a review."})
        elif k == "reviews":
            n = d.get("reviews", 0)
            out.append({"label": "Reviews", "passed": ok,
                        "finding": f"You have {n} Google review{'s' if n != 1 else ''}.",
                        "fix": "" if ok else f"Aim for at least {REVIEW_TARGET}. Put a QR code to your review link at the counter and send it with receipts or follow-up texts to customers who opted in."})
        elif k == "phone":
            out.append({"label": "Phone number", "passed": ok,
                        "finding": "Your phone number is listed." if ok else "No phone number is listed.",
                        "fix": "" if ok else "Add a phone number so mobile searchers can tap to call."})
        elif k == "ai":
            out.append({"label": "AI assistants", "passed": ok,
                        "finding": f"An AI assistant named {name} when asked for the best {category} in {city}." if ok else
                        f"When we asked an AI assistant for the best {category} in {city}, {name} wasn't mentioned.",
                        "fix": "" if ok else "AI assistants lean on websites, reviews and listings. A website plus more reviews and complete profile info give them something to cite."})
    out.sort(key=lambda x: x["passed"])
    return out


def ai_mention_check(name: str, category: str, city: str) -> bool | None:
    """Optional: ask Claude (with web search) for the best <category> in <city> and see if the
    business is named. Off by default (LOCAL_AI_CHECK) because each check costs API usage."""
    if not (config.LOCAL_AI_CHECK and config.ANTHROPIC_API_KEY):
        return None
    import anthropic
    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=config.CLAUDE_MODEL, max_tokens=4000, output_config={"effort": "low"},
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}],
            messages=[{"role": "user", "content": f"What are the best {category}s in {city}? List the top 10 by name only."}])
        if resp.stop_reason == "refusal":
            return None
        text = " ".join(b.text for b in resp.content if b.type == "text").lower()
        core = re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()
        return bool(core) and core in re.sub(r"[^a-z0-9 ]", "", text)
    except Exception as e:  # noqa: BLE001
        log.warning("AI check failed: %s", e)
        return None


# ------------------------------------------------------------------ mock site themes (stock, no Google photos)
THEMES = {
    "beauty": {"types": ["nail_salon", "beauty_salon", "hair_salon", "hair_care", "spa", "barber_shop"],
               "accent": "#c2477a", "bg": "#fff6f9", "tagline": "Look good. Feel better.",
               "services": ["Signature service", "Express appointment", "Deluxe treatment", "Walk-ins welcome", "Gift cards"],
               "blurb": "A friendly neighborhood studio focused on clean, careful work and a relaxing visit."},
    "barber": {"types": ["barber_shop"], "accent": "#1f4e79", "bg": "#f4f7fb", "tagline": "Sharp cuts. Easy chairs.",
               "services": ["Classic cut", "Skin fade", "Beard trim & line-up", "Hot towel shave", "Kids' cut"],
               "blurb": "Cuts done right, every time, by barbers who know your name."},
    "food": {"types": ["restaurant", "cafe", "bakery", "meal_takeaway", "bar", "coffee_shop", "pizza_restaurant"],
             "accent": "#b5531c", "bg": "#fffaf3", "tagline": "Made fresh, served with care.",
             "services": ["Dine in", "Takeout", "Catering", "Daily specials", "Private events"],
             "blurb": "Honest food made from good ingredients, in a place that feels like home."},
    "auto": {"types": ["car_repair", "car_wash", "auto_parts_store", "tire_shop", "car_dealer"],
             "accent": "#0f766e", "bg": "#f3faf9", "tagline": "Honest repairs. Fair prices.",
             "services": ["Diagnostics", "Brakes", "Oil change", "Tires & alignment", "A/C repair"],
             "blurb": "Straight answers and quality work to keep you on the road."},
    "health": {"types": ["dentist", "doctor", "physiotherapist", "chiropractor", "veterinary_care"],
               "accent": "#2563eb", "bg": "#f5f8ff", "tagline": "Care you can count on.",
               "services": ["New patients welcome", "Consultations", "Routine visits", "Follow-up care", "Insurance help"],
               "blurb": "Attentive, unhurried care from a team that puts you first."},
    "trades": {"types": ["plumber", "electrician", "roofing_contractor", "general_contractor", "locksmith", "painter"],
               "accent": "#c2410c", "bg": "#fff8f3", "tagline": "Done right the first time.",
               "services": ["Free estimates", "Repairs", "Installations", "Emergency service", "Maintenance plans"],
               "blurb": "Licensed pros who show up on time and leave the place clean."},
    "default": {"types": [], "accent": "#1e5ac8", "bg": "#f6f8fc", "tagline": "Proudly serving our neighbors.",
                "services": ["Our services", "Friendly team", "Fair prices", "Local and trusted", "Call for details"],
                "blurb": "A local business that takes pride in great service."},
}


_CATEGORY_THEME = [  # scan category / Google type words -> design + copy pack
    (("nail", "beauty", "salon", "spa", "lash", "brow", "hair"), "beauty"), (("barber",), "barber"),
    (("restaurant", "cafe", "bakery", "food", "pizza", "coffee", "bar", "grill", "kitchen"), "food"),
    (("auto", "car ", "car_", "tire", "mechanic", "car wash"), "auto"),
    (("dent", "doctor", "chiro", "physio", "clinic", "veterin", "health"), "health"),
    (("plumb", "electric", "roof", "contractor", "painter", "locksmith", "landscap", "lawn", "clean", "hvac",
      "heating", "moving", "handyman", "pest", "pool"), "trades"),
]


def theme_for(place_type: str, category: str = "") -> dict:
    for key, t in THEMES.items():
        if place_type in t["types"]:
            return {"key": key, **t}
    text = f"{place_type} {category}".lower().replace("_", " ")
    for words, key in _CATEGORY_THEME:
        if any(w in text for w in words):
            return {"key": key, **THEMES[key]}
    return {"key": "default", **THEMES["default"]}


def site_content(b, d: dict, owner: dict | None = None) -> dict:
    """Everything the premium site template renders: live Google details + what their own site says
    (redesigns) + what the owner confirmed (paid sites). See copywriter.py for the research behind it."""
    site = json.loads(b["site_json"]) if b and b["site_json"] else None
    pc = d.get("postal") or {}
    return copywriter.build(
        name=d["name"], theme_key=theme_for(d.get("type", ""), b["category"] if b else "")["key"],
        city=pc.get("city", ""), state=pc.get("state", ""), phone=d.get("phone"), address=d.get("address", ""),
        maps_uri=d.get("maps_uri"), hours=d.get("hours"), rating=d.get("rating"), reviews=d.get("reviews") or 0,
        site=site, owner=owner, type_label=d.get("type_label", ""))


# ------------------------------------------------------------------ scanning
def _searches_today() -> int:
    start = db.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return db.q1("SELECT COUNT(*) n FROM local_scan_log WHERE created_at>=?", (db.iso(start),))["n"]


def _is_dnc_place(place_id: str) -> bool:
    return bool(db.q1("SELECT 1 FROM dnc WHERE channel_id=?", ("place:" + place_id,)))


def build_business(d: dict, category: str, city: str, site: dict | None = None) -> int:
    ai = ai_mention_check(d["name"], category, city)
    score, checks = run_checks(d, ai)
    now = db.now()
    bid = db.insert("businesses", {
        "place_id": d["place_id"], "category": category, "city": city, "stage": "built", "token": db.token(),
        "score": score, "checks_json": json.dumps(checks), "ai_mentioned": None if ai is None else int(ai),
        "kind": "redesign" if site else "no_website", "site_url": (site or {}).get("final_url"),
        "site_json": json.dumps(site) if site else None, "site_score": (site or {}).get("score"),
        "expires_at": db.iso(now + timedelta(days=config.MOCK_EXPIRY_DAYS)),
        "created_at": db.iso(now), "updated_at": db.iso(now)})
    if site:  # contact details THEY publish on their own website
        socials = site.get("socials") or {}
        try:
            set_social(bid, socials.get("facebook", ""), socials.get("instagram", ""), site["final_url"])
        except ValueError:
            pass
        for email in site.get("emails") or []:
            if not emailer.is_dnc(email):
                set_contact(bid, email, f"published on their website ({site['final_url']})",
                            approve=config.REDESIGN_AUTO_SEND)
                break
    return bid


def daily_scan(max_searches: int | None = None) -> dict:
    pc = places.client()
    if not pc:
        return {"skipped": "GOOGLE_PLACES_API_KEY not set"}
    budget = min(max_searches or config.LOCAL_SEARCHES_PER_DAY, config.LOCAL_SEARCHES_PER_DAY) - _searches_today()
    # fixed shuffled order: consecutive days hit different cities and trades across the country
    pairs = sorted(itertools.product(config.LOCAL_CATEGORIES, config.LOCAL_CITIES),
                   key=lambda p: hashlib.sha1(f"{p[0]}|{p[1]}".encode()).hexdigest())
    stats = {"searches": 0, "found": 0, "no_website": 0, "kept": 0, "sites_read": 0, "redesigns": 0, "emails_found": 0}
    if budget <= 0 or not pairs:
        return stats
    start = db.q1("SELECT COUNT(*) n FROM local_scan_log")["n"]
    t_start = time.monotonic()
    for i in range(budget):
        category, city = pairs[(start + i) % len(pairs)]
        query = f"{category} {city}"
        try:
            results = pc.text_search(query)
        except places.PlacesError as e:
            log.error("places search failed: %s", e)
            break
        found = len(results)
        no_site = kept = 0
        for d in results:
            if d["status"] != "OPERATIONAL":
                continue
            if db.q1("SELECT 1 FROM businesses WHERE place_id=?", (d["place_id"],)) or _is_dnc_place(d["place_id"]):
                continue
            site = None
            if not d["website"]:
                no_site += 1
            elif (config.REDESIGN_ENABLED and stats["sites_read"] < config.SITECHECKS_PER_SCAN
                  and time.monotonic() - t_start < config.SITECHECK_BUDGET_S):
                stats["sites_read"] += 1
                site = sitecheck.safe_analyze(d["website"])
                if not site or site["score"] >= config.REDESIGN_MAX_SCORE:
                    continue  # unreadable, or already a decent site: nothing honest to pitch
                stats["redesigns"] += 1
                stats["emails_found"] += bool(site.get("emails"))
            elif not (config.LOCAL_TARGET == "no_website_or_weak" and run_checks(d, None)[0] < 60):
                continue
            build_business(d, category, city, site)
            kept += 1
        db.insert("local_scan_log", {"category": category, "city": city, "query": query, "found": found,
                                     "no_website": no_site, "kept": kept, "created_at": db.iso()})
        stats["searches"] += 1
        stats["found"] += found
        stats["no_website"] += no_site
        stats["kept"] += kept
    log.info("local scan: %s", stats)
    return stats


# ------------------------------------------------------------------ helpers used by pages
def get(token: str):
    return db.q1("SELECT * FROM businesses WHERE token=?", (token,))


def live_details(b) -> dict | None:
    """Fresh details from Google, or None (no key, outage, or the monthly free cap is used up)."""
    pc = places.client()
    if not pc:
        return None
    try:
        return pc.details(b["place_id"])
    except places.PlacesError as e:
        log.warning("place details unavailable: %s", e)
        return None


def preview_context(b, d: dict) -> dict:
    checks = json.loads(b["checks_json"] or "[]")
    # re-score from live details so the page always matches what Google shows today
    ai = None if b["ai_mentioned"] is None else bool(b["ai_mentioned"])
    score, checks = run_checks(d, ai)
    site = json.loads(b["site_json"]) if b["site_json"] else None
    if site:  # they have a website: the listing check shouldn't fail them for "no website"
        checks = [c for c in checks if c["key"] != "website"]
        score = round((score + site["score"]) / 2)
    return {"b": b, "d": d, "score": score, "insights": insights(d, checks, b["category"], b["city"]),
            "theme": theme_for(d.get("type", ""), b["category"]), "site": site,
            "site_fails": [c for c in (site or {}).get("checks", []) if not c["passed"]]}


def record_visit(b) -> None:
    db.ex("UPDATE businesses SET visits=visits+1, first_visit_at=COALESCE(first_visit_at, ?), updated_at=?, "
          "stage=CASE WHEN stage IN ('built','contact_found','approved','contacted') THEN 'visited' ELSE stage END "
          "WHERE id=?", (db.iso(), db.iso(), b["id"]))


def is_expired(b) -> bool:
    exp = db.parse_iso(b["expires_at"])
    return b["stage"] in ("expired", "optout") or (exp is not None and exp < db.now() and not b["paid_at"])


def optout(b, reason: str = "opt-out link") -> None:
    db.ex("INSERT OR IGNORE INTO dnc (email, channel_id, reason, created_at) VALUES (?,?,?,?)",
          (b["contact_email"].lower() if b["contact_email"] else None, "place:" + b["place_id"], reason, db.iso()))
    db.update("businesses", b["id"], {"stage": "optout", "updated_at": db.iso()})


def _mark_contacted(b, via: str) -> None:
    prev = b["contacted_via"]
    both = "both" if prev and prev != via else via
    db.ex("UPDATE businesses SET contacted_via=?, updated_at=?, expires_at=?, "
          "stage=CASE WHEN stage IN ('built','contact_found','approved') THEN 'contacted' ELSE stage END WHERE id=?",
          (both, db.iso(), db.iso(db.now() + timedelta(days=config.MOCK_EXPIRY_DAYS)), b["id"]))


# ------------------------------------------------------------------ contacts (email)
def set_contact(bid: int, email: str, source: str, approve: bool = False) -> None:
    from .outreach import EMAIL_RE
    email = email.strip()
    if not EMAIL_RE.match(email):
        raise ValueError("That doesn't look like an email address")
    if not source.strip():
        raise ValueError("Record where the business published this email (Facebook, Instagram, Yelp, lead source)")
    if emailer.is_dnc(email):
        raise ValueError("That email is on the do-not-contact list")
    b = db.q1("SELECT * FROM businesses WHERE id=?", (bid,))
    stage = b["stage"]
    if stage in ("built", "contact_found"):
        stage = "approved" if approve else "contact_found"
    db.update("businesses", bid, {"contact_email": email, "contact_source": source.strip(), "stage": stage,
                                  "approved_at": db.iso() if approve else b["approved_at"], "updated_at": db.iso()})


_SOCIAL = {"facebook_url": ("facebook.com", "fb.com", "m.facebook.com"), "instagram_url": ("instagram.com",)}


def clean_social(url: str, kind: str) -> str | None:
    """Normalise a Facebook/Instagram profile link; None if blank, ValueError if it isn't one."""
    u = (url or "").strip()
    if not u:
        return None
    if u.startswith("@") and kind == "instagram_url":
        u = "instagram.com/" + u[1:]
    if not u.startswith("http"):
        u = "https://" + u
    host = u.split("://", 1)[1].split("/", 1)[0].lower().removeprefix("www.")
    path = u.split("://", 1)[1].partition("/")[2].split("?")[0].strip("/")
    if host not in _SOCIAL[kind] or not path:
        raise ValueError(f"That isn't a {'Facebook' if kind == 'facebook_url' else 'Instagram'} profile link")
    return f"https://www.{_SOCIAL[kind][0]}/{path}"


def set_social(bid: int, facebook: str = "", instagram: str = "", source: str = "") -> None:
    """Store the social profiles a business publishes (kept even without an email, for later contact)."""
    fb, ig = clean_social(facebook, "facebook_url"), clean_social(instagram, "instagram_url")
    if not (fb or ig):
        return
    upd = {"social_source": source.strip() or "admin", "updated_at": db.iso()}
    if fb:
        upd["facebook_url"] = fb
    if ig:
        upd["instagram_url"] = ig
    db.update("businesses", bid, upd)


def import_leads(raw: bytes, default_source: str) -> dict:
    """CSV columns: place_id, email, source, facebook, instagram. Email and/or social links; only ones the
    business published. Rows with socials but no email are kept for future social contact."""
    import csv
    import io
    res = {"updated": 0, "errors": []}
    for i, r in enumerate(csv.DictReader(io.StringIO(raw.decode("utf-8-sig", errors="replace"))), 2):
        r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
        b = db.q1("SELECT id FROM businesses WHERE place_id=?", (r.get("place_id", ""),))
        if not b:
            res["errors"].append(f"row {i}: unknown place_id")
            continue
        try:
            src = r.get("source") or default_source
            set_social(b["id"], r.get("facebook", ""), r.get("instagram", ""), src)
            if r.get("email"):
                set_contact(b["id"], r["email"], src, approve=config.OUTREACH_AUTO_APPROVE_IMPORTS)
            elif not (r.get("facebook") or r.get("instagram")):
                raise ValueError("needs an email, facebook or instagram")
            res["updated"] += 1
        except ValueError as e:
            res["errors"].append(f"row {i}: {e}")
    return res


def _h(s) -> str:
    from markupsafe import escape
    return str(escape(s or ""))


# ------------------------------------------------------------------ emails
def _redesign_email(b, d: dict, ctx: dict, followup: bool) -> tuple[str, str, str]:
    """Pitch for businesses that already have a website: what we found on it, plus the redesign we built."""
    link = config.link(f"/b/{b['token']}")
    name, site = d["name"], ctx["site"]
    fails = ctx["site_fails"][:3]
    domain = site.get("domain", "your website").removeprefix("www.")
    if not followup:
        subject = f"A redesign for {domain}"
        items = "".join(f"<li>{_h(i['finding'])}</li>" for i in fails)
        html = (f"<p>Hi {_h(name)} team,</p><p>I'm with {config.BRAND}. I looked at {_h(domain)} the way a new customer "
                f"would, on a phone, and found a few things that cost you calls (site score <b>{site['score']}/100</b>):</p>"
                f"<ul>{items}</ul><p>So we rebuilt it as a free, private preview using your own name, services and contact "
                f"details: fast, built for phones, with your Google reviews up front and a tap-to-call button on every screen.</p>"
                f"<p>See the redesign and the full check-up here:<br><a href='{link}'>{link}</a></p>"
                f"<p>If you like it, it can be live within the hour for ${config.PRICE_WEBSITE // 100}, or we can do everything "
                f"for you, domain included. If not, no worries.</p><p>— The {config.BRAND} team</p>")
        text = (f"Hi {name} team,\n\nI looked at {domain} the way a new customer would, on a phone, and found a few things "
                f"that cost you calls (site score {site['score']}/100):\n" + "".join(f"- {i['finding']}\n" for i in fails)
                + f"\nWe rebuilt it as a free, private preview with your own details: {link}\n\n- The {config.BRAND} team")
    else:
        subject = f"Re: A redesign for {domain}"
        html = (f"<p>Quick follow-up: the redesign we built for {_h(name)} is still here: <a href='{link}'>{link}</a></p>"
                f"<p>It expires soon. This is the last email unless you reply.</p><p>— The {config.BRAND} team</p>")
        text = f"Quick follow-up: the redesign we built for {name} is still here: {link}\nThis is the last email unless you reply."
    return subject, html.replace("'", '"'), text


def _business_email(b, d: dict, ctx: dict, followup: bool) -> tuple[str, str, str]:
    if ctx.get("site"):
        return _redesign_email(b, d, ctx, followup)
    link = config.link(f"/b/{b['token']}")
    name = d["name"]
    fails = [i for i in ctx["insights"] if not i["passed"]][:3]
    if not followup:
        subject = f"We built {name} a free website preview"
        items = "".join(f"<li>{_h(i['finding'])}</li>" for i in fails)
        html = (f"<p>Hi {_h(name)} team,</p><p>I'm with {config.BRAND}. We noticed {_h(name)} doesn't have a website on its "
                f"Google listing, so we built a preview of one and ran a quick check-up of your listing "
                f"(score: <b>{ctx['score']}/100</b>):</p><ul>{items}</ul>"
                f"<p>See your site preview and the full check-up here (private link):<br><a href='{link}'>{link}</a></p>"
                f"<p>If you like it, it can be live within the hour for ${config.PRICE_WEBSITE // 100}. If not, no worries.</p>"
                f"<p>— The {config.BRAND} team</p>")
        text = (f"Hi {name} team,\n\nWe noticed {name} doesn't have a website on its Google listing, so we built a preview "
                f"and ran a quick check-up of your listing (score: {ctx['score']}/100):\n"
                + "".join(f"- {i['finding']}\n" for i in fails)
                + f"\nYour preview (private link): {link}\n\n- The {config.BRAND} team")
    else:
        subject = f"Re: We built {name} a free website preview"
        html = (f"<p>Quick follow-up: your website preview for {_h(name)} is still here: <a href='{link}'>{link}</a></p>"
                f"<p>It expires soon. This is the last email unless you reply.</p><p>— The {config.BRAND} team</p>")
        text = f"Quick follow-up: your website preview for {name} is still here: {link}\nThis is the last email unless you reply."
    return subject, html.replace("'", '"'), text


def send_emails() -> dict:
    from .outreach import _sent_today
    res = {"sent": 0, "followups": 0, "blocked": 0}
    room = config.OUTREACH_DAILY_SEND_LIMIT - _sent_today()
    cutoff = db.iso(db.now() - timedelta(days=config.OUTREACH_FOLLOWUP_DAYS))
    first = db.q("SELECT * FROM businesses WHERE stage IN ('approved','contacted') AND contact_email IS NOT NULL "
                 "AND approved_at IS NOT NULL AND emails_sent=0 ORDER BY approved_at")
    follow = db.q("SELECT * FROM businesses WHERE stage='contacted' AND emails_sent BETWEEN 1 AND ? AND last_emailed_at<=? "
                  "AND first_visit_at IS NULL", (config.OUTREACH_MAX_FOLLOWUPS, cutoff))
    for b, is_follow in [(b, False) for b in first] + [(b, True) for b in follow]:
        if room <= 0:
            break
        if not in_local_send_hours(b["city"]):
            continue  # nationwide: wait for business hours in the business's own time zone
        if emailer.is_dnc(b["contact_email"], "place:" + b["place_id"]) or is_expired(b):
            res["blocked"] += 1
            continue
        d = live_details(b)
        if not d:
            continue
        subject, html, text = _business_email(b, d, preview_context(b, d), is_follow)
        try:
            eid = emailer.send("outreach_followup" if is_follow else "outreach", b["contact_email"], subject, html, text,
                               channel_id="place:" + b["place_id"])
        except emailer.SendBlocked:
            res["blocked"] += 1
            continue
        db.update("emails", eid, {"business_id": b["id"]})
        db.ex("UPDATE businesses SET emails_sent=emails_sent+1, last_emailed_at=? WHERE id=?", (db.iso(), b["id"]))
        _mark_contacted(b, "email")
        res["followups" if is_follow else "sent"] += 1
        room -= 1
    return res


def local_hour(city: str | None) -> int:
    """Hour of day where the business is (standard-time offset from its state; US Eastern if unknown)."""
    from .us_cities import STATE_UTC
    state = (city or "").strip().split(" ")[-1].upper()
    off = STATE_UTC.get(state, -5)
    if 3 <= db.now().month <= 10:  # rough daylight saving (Mar-Oct); AZ and HI don't change
        off += 0 if state in ("AZ", "HI") else 1
    return (db.now().hour + off) % 24


def in_local_send_hours(city: str | None) -> bool:
    return config.OUTREACH_SEND_HOURS[0] <= local_hour(city) < config.OUTREACH_SEND_HOURS[1]


def send_due(ignore_hours: bool = False) -> dict:
    """Runs every scheduler pass; each business is only emailed inside its own local business hours."""
    if ignore_hours:
        old, config.OUTREACH_SEND_HOURS = config.OUTREACH_SEND_HOURS, (0, 24)
        try:
            return {"emails": send_emails()}
        finally:
            config.OUTREACH_SEND_HOURS = old
    return {"emails": send_emails()}


# ------------------------------------------------------------------ tracking + retention
def funnel() -> list[dict]:
    scans = {(r["category"], r["city"]): dict(r) for r in db.q(
        "SELECT category, city, SUM(found) scanned, SUM(no_website) no_website, SUM(kept) built FROM local_scan_log "
        "GROUP BY category, city")}
    rows = db.q("SELECT category, city, SUM(emails_sent>0) emailed, "
                "SUM(first_visit_at IS NOT NULL) visited, SUM(consult_clicked_at IS NOT NULL) consult, "
                "SUM(paid_at IS NOT NULL) paid, SUM(stage='optout') optout FROM businesses GROUP BY category, city")
    out = []
    for r in rows:
        s = scans.get((r["category"], r["city"]), {})
        out.append({"category": r["category"], "city": r["city"], "scanned": s.get("scanned", 0),
                    "no_website": s.get("no_website", 0), "built": s.get("built", 0), **{k: r[k] for k in
                    ("emailed", "visited", "consult", "paid", "optout")}})
    return out


def expire_and_purge() -> dict:
    now = db.iso()
    n_exp = db.ex("UPDATE businesses SET stage='expired', updated_at=? WHERE expires_at<? AND paid_at IS NULL "
                  "AND stage NOT IN ('expired','optout','paid')", (now, now))
    cutoff = db.iso(db.now() - timedelta(days=30))
    cur = db.conn().execute("DELETE FROM businesses WHERE stage='expired' AND updated_at<?", (cutoff,))
    db.conn().commit()
    return {"deleted": cur.rowcount}


# ------------------------------------------------------------------ self-serve preview
class LookupLimited(Exception):
    pass


def _lookups_since(hours: int, ip: str | None = None) -> int:
    since = db.iso(db.now() - timedelta(hours=hours))
    if ip:
        return db.q1("SELECT COUNT(*) n FROM lookups WHERE created_at>=? AND ip=?", (since, ip))["n"]
    return db.q1("SELECT COUNT(*) n FROM lookups WHERE created_at>=?", (since,))["n"]


def _check_limits(ip: str) -> None:
    if not places.client() or not config.SELF_SERVE_ENABLED:
        raise LookupLimited("The free preview isn't available right now.")
    if _lookups_since(24, ip) >= config.SELF_SERVE_PER_IP_PER_DAY:
        raise LookupLimited("You've reached today's limit for free previews. Try again tomorrow.")
    if _lookups_since(24) >= config.SELF_SERVE_PER_DAY:
        raise LookupLimited("We've hit today's limit for free previews. Try again tomorrow.")


def self_serve_search(name: str, city: str, ip: str) -> list[dict]:
    """A business owner types their name + city. One Places Text Search call, rate-limited per visitor
    and per day overall so the free tier isn't exceeded. Results are shown once, not stored."""
    _check_limits(ip)
    pc = places.client()
    query = f"{name.strip()} {city.strip()}"[:120]
    try:
        results = pc.text_search(query, page_size=5)
    except places.PlacesCapReached:
        raise LookupLimited("Free previews are paused for now. Try again in a few days.") from None
    db.insert("lookups", {"ip": ip, "query": query, "result": str(len(results)), "created_at": db.iso()})
    return [d for d in results if d.get("status") == "OPERATIONAL"][:5]


def self_serve_build(place_id: str, ip: str) -> str:
    """Create (or reuse) the check-up + mock site for a place the visitor picked. Returns the token."""
    if _is_dnc_place(place_id):
        raise LookupLimited("This business asked us not to build a preview.")
    b = db.q1("SELECT * FROM businesses WHERE place_id=?", (place_id,))
    if b:
        if b["stage"] == "optout":
            raise LookupLimited("This business asked us not to build a preview.")
        if b["stage"] == "expired":  # bring it back for another round
            db.update("businesses", b["id"], {"stage": "built", "updated_at": db.iso(),
                                              "expires_at": db.iso(db.now() + timedelta(days=config.MOCK_EXPIRY_DAYS))})
        return b["token"]
    _check_limits(ip)
    pc = places.client()
    try:
        d = pc.details(place_id)
    except places.PlacesError:
        raise LookupLimited("Free previews are paused for now. Try again in a few days.") from None
    db.insert("lookups", {"ip": ip, "query": "details:" + place_id[:80], "result": "1" if d else "0",
                          "created_at": db.iso()})
    if not d:
        raise LookupLimited("We couldn't load that business from Google. Try the search again.")
    pa = d.get("postal") or {}
    city = " ".join(x for x in (pa.get("city"), pa.get("state")) if x) or ""
    category = (d.get("type_label") or d.get("type") or "business").lower()
    bid = build_business(d, category, city)
    db.update("businesses", bid, {"source": "self_serve"})
    return db.q1("SELECT token FROM businesses WHERE id=?", (bid,))["token"]


# ------------------------------------------------------------------ 30-day plan (paid website buyers)
_PLAN_STEPS = {
    # check key -> (week, task) used when that check failed; fixed text, no AI-written numbers
    "website": (1, "Add your new website as the 'Website' on your Google Business Profile (Edit profile > Contact)."),
    "hours": (1, "Fill in your regular hours and any holiday hours on your Google Business Profile."),
    "phone": (1, "Add your main phone number to your Google Business Profile so mobile searchers can tap to call."),
    "photos": (2, f"Upload at least {PHOTO_TARGET} clear photos: storefront, inside, your work and your team."),
    "rating": (3, "Reply to every review from the last 6 months, good and bad. Short, polite and specific."),
    "reviews": (3, f"Ask every happy customer this week for a Google review (counter QR code + a line on receipts). Goal: {REVIEW_TARGET}+ reviews."),
    "ai": (4, "Make sure your name, address and phone are exactly the same on your website, Google, Facebook and Yelp."),
}
_PLAN_ALWAYS = [
    (1, "Check your new website on your phone: tap-to-call, directions and hours all work."),
    (2, "Post one Google Business Profile update (an offer, a new service or a photo of recent work)."),
    (3, "Share your website link on your Facebook / Instagram profile and pin it."),
    (4, "Post another Google update and add 3 new photos. Keep doing this every month."),
    (4, "Day 30 check-in: compare calls, website clicks and direction requests in your Google Business Profile insights with last month."),
]
_WEEK_THEMES = {1: "Week 1: get found (days 1-7)", 2: "Week 2: look active (days 8-14)",
                3: "Week 3: build trust (days 15-21)", 4: "Week 4: keep it going (days 22-30)"}


def plan_30(insights_list: list[dict]) -> list[dict]:
    """Weekly to-do list built from this business's failed checks plus habits everyone needs."""
    label_key = {"Website": "website", "Opening hours": "hours", "Photos": "photos", "Rating": "rating",
                 "Reviews": "reviews", "Phone number": "phone", "AI assistants": "ai"}
    keys = {label_key.get(i["label"]) for i in insights_list if not i["passed"]}
    rows = [(w, t) for k, (w, t) in _PLAN_STEPS.items() if k in keys] + _PLAN_ALWAYS
    return [{"week": w, "title": _WEEK_THEMES[w], "tasks": [t for ww, t in rows if ww == w]} for w in sorted(_WEEK_THEMES)]
