"""Outreach: daily scanner -> teaser audits -> contact review -> automatic sending.

Contact emails are NEVER pulled from YouTube (no API harvesting, no About-page scraping).
They come from a person or a lead source, each with a recorded source, via the admin
review queue or a lead CSV import. Everything after that is automatic: first email,
one follow-up, open/click tracking, unsubscribe -> do-not-contact, funnel logging.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import statistics
from datetime import timedelta

from . import config, db, emailer, youtube

log = logging.getLogger("outreach")

QUERY_VARIANTS = ["{n}", "{n} tips", "{n} for beginners", "{n} vlog", "{n} challenge", "{n} routine",
                  "{n} review", "{n} tutorial", "{n} ideas", "{n} mistakes"]
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)


def _searches_today() -> int:
    start = db.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return db.q1("SELECT COUNT(*) n FROM search_log WHERE created_at>=?", (db.iso(start),))["n"]


def _new_prospect(ch: dict, niche: str, recent: float | None, earlier: float | None, last_upload) -> int:
    now = db.now()
    pid = db.insert("prospects", {
        "channel_id": ch["id"], "channel_title": ch["title"], "niche": niche, "subscribers": ch["subscribers"],
        "last_upload_at": last_upload, "recent_median": recent, "earlier_median": earlier, "stage": "scanned",
        "teaser_token": db.token(), "teaser_expires_at": db.iso(now + timedelta(days=config.TEASER_EXPIRY_DAYS)),
        "api_data_fetched_at": db.iso(now), "created_at": db.iso(now), "updated_at": db.iso(now)})
    jid = db.insert("jobs", {"kind": "teaser", "tier": "teaser", "teaser_id": pid, "channel_id": ch["id"],
                             "channel_title": ch["title"], "status": "queued", "created_at": db.iso()})
    db.update("prospects", pid, {"teaser_job_id": jid})
    return pid


def evaluate_channel(yt: youtube.YouTube, ch: dict) -> tuple[bool, dict]:
    """Starting filters (all adjustable in config): active in last N days, subscriber range,
    recent median views falling against the channel's own earlier median."""
    subs = ch.get("subscribers")
    if subs is None or not (config.OUTREACH_MIN_SUBS <= subs <= config.OUTREACH_MAX_SUBS):
        return False, {"reason": "subs"}
    vids = yt.videos(yt.upload_ids(ch["uploads_playlist"], limit=30))
    vids.sort(key=lambda v: v["published"], reverse=True)
    if len(vids) < 12:
        return False, {"reason": "few_videos"}
    last = db.parse_iso(vids[0]["published"])
    if (db.now() - last).days > config.OUTREACH_ACTIVE_DAYS:
        return False, {"reason": "inactive"}
    recent = statistics.median(v["views_public"] for v in vids[:10])
    earlier = statistics.median(v["views_public"] for v in vids[10:20])
    info = {"recent": recent, "earlier": earlier, "last_upload": vids[0]["published"]}
    if earlier <= 0 or recent >= config.OUTREACH_DECLINE_RATIO * earlier:
        return False, {"reason": "not_declining", **info}
    return True, info


def daily_scan(max_searches: int | None = None) -> dict:
    yt = youtube.client()
    if not yt:
        log.warning("scanner skipped: YOUTUBE_API_KEY not set")
        return {"skipped": "no api key"}
    budget = min(max_searches or config.OUTREACH_SEARCHES_PER_DAY, config.OUTREACH_SEARCHES_PER_DAY) - _searches_today()
    stats = {"searches": 0, "channels_seen": 0, "kept": 0}
    niches = config.OUTREACH_NICHES
    if budget <= 0 or not niches:
        return stats
    per_niche = max(1, budget // len(niches))
    for niche in niches:
        done = db.q1("SELECT COUNT(*) n FROM search_log WHERE niche=?", (niche,))["n"]
        for i in range(per_niche):
            if stats["searches"] >= budget:
                break
            q =QUERY_VARIANTS[(done + i) % len(QUERY_VARIANTS)].format(n=niche)
            try:
                ids = yt.search_active_channels(q, config.OUTREACH_ACTIVE_DAYS, config.OUTREACH_RESULTS_PER_SEARCH)
            except youtube.YouTubeError as e:
                log.error("search failed: %s", e)
                return stats
            db.insert("search_log", {"niche": niche, "query": q, "results": len(ids), "created_at": db.iso()})
            stats["searches"] += 1
            known = {r["channel_id"] for r in db.q(
                f"SELECT channel_id FROM prospects WHERE channel_id IN ({','.join('?' * len(ids))})", ids)} if ids else set()
            fresh = [c for c in ids if c not in known and not emailer.is_dnc(None, c)]
            for ch in yt.channels(fresh):
                stats["channels_seen"] += 1
                ok, info = evaluate_channel(yt, ch)
                if ok:
                    _new_prospect(ch, niche, info["recent"], info["earlier"], info["last_upload"])
                    stats["kept"] += 1
    stats["quota_units"] = yt.units_used
    log.info("scan done: %s", stats)
    return stats


def add_channel(channel_ref: str, niche: str = "manual") -> int:
    """Add one channel by URL/ID (e.g. from a lead source) and queue its teaser."""
    yt = youtube.client()
    if not yt:
        raise RuntimeError("YOUTUBE_API_KEY is not set")
    cid = yt.resolve_channel(channel_ref)
    row = db.q1("SELECT id FROM prospects WHERE channel_id=?", (cid,))
    if row:
        return row["id"]
    ch = yt.channels([cid])[0]
    return _new_prospect(ch, niche, None, None, None)


# ------------------------------------------------------------------ contacts
def set_contact(pid: int, email: str, source: str, approve: bool = False) -> None:
    email = email.strip()
    if not EMAIL_RE.match(email):
        raise ValueError("That doesn't look like an email address")
    if not source.strip():
        raise ValueError("Record where the email was published (their website, link-in-bio page, lead source)")
    if emailer.is_dnc(email):
        raise ValueError("That email is on the do-not-contact list")
    p = db.q1("SELECT * FROM prospects WHERE id=?", (pid,))
    stage = p["stage"]
    if stage in ("scanned", "teaser_built", "contact_found", "skipped"):
        stage = "approved" if approve else "contact_found"
    db.update("prospects", pid, {"contact_email": email, "contact_source": source.strip(), "stage": stage,
                                 "approved_at": db.iso() if approve else None, "updated_at": db.iso()})


def approve(pid: int) -> None:
    db.ex("UPDATE prospects SET stage='approved', approved_at=?, updated_at=? WHERE id=? AND contact_email IS NOT NULL "
          "AND stage IN ('contact_found','teaser_built')", (db.iso(), db.iso(), pid))


def skip(pid: int) -> None:
    db.ex("UPDATE prospects SET stage='skipped', updated_at=? WHERE id=? AND stage NOT IN ('emailed','opened','clicked','paid')",
          (db.iso(), pid))


def import_leads(raw: bytes, default_source: str) -> dict:
    """CSV columns: channel (URL or ID), email, source (optional). Each row's email must be one
    the creator published themselves; the source column records where."""
    text = raw.decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    res = {"added": 0, "updated": 0, "errors": []}
    for i, r in enumerate(rows, 2):
        r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
        ch = r.get("channel") or r.get("channel_url") or r.get("channel_id")
        email = r.get("email", "")
        source = r.get("source") or default_source
        if not ch or not email:
            res["errors"].append(f"row {i}: needs channel and email")
            continue
        try:
            existed = bool(db.q1("SELECT 1 FROM prospects WHERE channel_id=?", (ch,)))
            pid = add_channel(ch, r.get("niche") or "import")
            set_contact(pid, email, source, approve=config.OUTREACH_AUTO_APPROVE_IMPORTS)
            res["updated" if existed else "added"] += 1
        except Exception as e:  # noqa: BLE001
            res["errors"].append(f"row {i}: {e}")
    return res


# ------------------------------------------------------------------ sending
def _teaser_bits(p) -> dict | None:
    if not p["teaser_job_id"]:
        return None
    job = db.q1("SELECT status FROM jobs WHERE id=?", (p["teaser_job_id"],))
    a = db.q1("SELECT * FROM audit_log WHERE job_id=? ORDER BY id DESC LIMIT 1", (p["teaser_job_id"],))
    if not job or job["status"] != "done" or not a or not a["narrative_json"]:
        return None
    import json
    nar = json.loads(a["narrative_json"])
    prob = nar["problems"][0] if nar.get("problems") else None
    return {"score": int(a["score"]), "problem": prob}


def _outreach_email(p, bits: dict, followup: bool) -> tuple[str, str, str]:
    link = config.link(f"/t/{p['teaser_token']}")
    title = p["channel_title"]
    prob = bits["problem"]
    if not followup:
        subject = f"Free audit of {title}: your channel scores {bits['score']}/100"
        html = (f"<p>Hi {title},</p>"
                f"<p>I run {config.BRAND}, a video editing studio. I ran a quick audit of your channel from public "
                f"YouTube data and it scored <b>{bits['score']}/100</b>.</p>"
                f"<p>The biggest thing holding it back: <b>{prob['title']}</b>. {prob['evidence']}</p>"
                f"<p>Your teaser report (private link, expires in {config.TEASER_EXPIRY_DAYS} days):<br>"
                f"<a href='{link}'>{link}</a></p>"
                f"<p>No strings attached. If it's useful, the full audit is there too.</p>"
                f"<p>— The {config.BRAND} team</p>")
        text = (f"Hi {title},\n\nI run {config.BRAND}, a video editing studio. I ran a quick audit of your channel from "
                f"public YouTube data and it scored {bits['score']}/100.\n\nThe biggest thing holding it back: "
                f"{prob['title']}. {prob['evidence']}\n\nYour teaser report (private link): {link}\n\n"
                f"No strings attached.\n\n- The {config.BRAND} team")
    else:
        subject = f"Re: Free audit of {title}"
        html = (f"<p>Hi {title}, quick follow-up on the free audit I sent. The one fix I'd start with: "
                f"{prob['fix']}</p><p>Your teaser is still here: <a href='{link}'>{link}</a></p>"
                f"<p>This is the last email unless you reply.</p><p>— The {config.BRAND} team</p>")
        text = (f"Hi {title}, quick follow-up on the free audit I sent. The one fix I'd start with: {prob['fix']}\n\n"
                f"Your teaser: {link}\n\nThis is the last email unless you reply.\n\n- The {config.BRAND} team")
    return subject, html.replace("'", '"'), text


def _sent_today() -> int:
    start = db.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return db.q1("SELECT COUNT(*) n FROM emails WHERE kind IN ('outreach','outreach_followup') AND status='sent' "
                 "AND created_at>=?", (db.iso(start),))["n"]


def send_due(ignore_hours: bool = False) -> dict:
    h = db.now().astimezone().hour
    if not ignore_hours and not (config.OUTREACH_SEND_HOURS[0] <= h < config.OUTREACH_SEND_HOURS[1]):
        return {"skipped": "outside send hours"}
    room = config.OUTREACH_DAILY_SEND_LIMIT - _sent_today()
    res = {"sent": 0, "followups": 0, "blocked": 0, "waiting_on_teaser": 0}
    if room <= 0:
        return res
    cutoff = db.iso(db.now() - timedelta(days=config.OUTREACH_FOLLOWUP_DAYS))
    first = db.q("SELECT * FROM prospects WHERE stage='approved' AND contact_email IS NOT NULL ORDER BY approved_at")
    follow = db.q("SELECT * FROM prospects WHERE stage IN ('emailed','opened') AND emails_sent BETWEEN 1 AND ? "
                  "AND last_emailed_at<=? AND clicked_at IS NULL AND paid_at IS NULL ORDER BY last_emailed_at",
                  (config.OUTREACH_MAX_FOLLOWUPS, cutoff))
    for p, is_follow in [(p, False) for p in first] + [(p, True) for p in follow]:
        if room <= 0:
            break
        if emailer.is_dnc(p["contact_email"], p["channel_id"]):  # checked before every send
            db.update("prospects", p["id"], {"stage": "unsubscribed", "updated_at": db.iso()})
            res["blocked"] += 1
            continue
        exp = db.parse_iso(p["teaser_expires_at"])
        if exp and exp < db.now() + timedelta(days=3):
            db.update("prospects", p["id"], {"teaser_expires_at": db.iso(db.now() + timedelta(days=config.TEASER_EXPIRY_DAYS))})
        bits = _teaser_bits(p)
        if not bits or not bits["problem"]:
            res["waiting_on_teaser"] += 1
            continue
        subject, html, text = _outreach_email(p, bits, is_follow)
        try:
            emailer.send("outreach_followup" if is_follow else "outreach", p["contact_email"], subject, html, text,
                         prospect_id=p["id"], channel_id=p["channel_id"])
        except emailer.SendBlocked:
            res["blocked"] += 1
            continue
        except Exception as e:  # noqa: BLE001
            log.error("send to prospect %s failed: %s", p["id"], e)
            continue
        db.ex("UPDATE prospects SET emails_sent=emails_sent+1, last_emailed_at=?, updated_at=?, "
              "stage=CASE WHEN stage='approved' THEN 'emailed' ELSE stage END WHERE id=?",
              (db.iso(), db.iso(), p["id"]))
        res["followups" if is_follow else "sent"] += 1
        room -= 1
    return res


# ------------------------------------------------------------------ events
STAGE_ORDER = ["scanned", "teaser_built", "contact_found", "approved", "emailed", "opened", "clicked", "paid"]


def advance(pid: int | None, stage: str, ts_field: str | None = None) -> None:
    if not pid:
        return
    p = db.q1("SELECT stage FROM prospects WHERE id=?", (pid,))
    if not p:
        return
    fields = {"updated_at": db.iso()}
    if ts_field:
        db.ex(f"UPDATE prospects SET {ts_field}=COALESCE({ts_field}, ?) WHERE id=?", (db.iso(), pid))
    if p["stage"] in STAGE_ORDER and STAGE_ORDER.index(stage) > STAGE_ORDER.index(p["stage"]):
        fields["stage"] = stage
    db.update("prospects", pid, fields)


def unsubscribe(email_token: str) -> str | None:
    e = db.q1("SELECT * FROM emails WHERE token=?", (email_token,))
    if not e:
        return None
    channel_id = None
    if e["prospect_id"]:
        p = db.q1("SELECT channel_id FROM prospects WHERE id=?", (e["prospect_id"],))
        channel_id = p["channel_id"] if p else None
        db.update("prospects", e["prospect_id"], {"stage": "unsubscribed", "updated_at": db.iso()})
    if "business_id" in e.keys() and e["business_id"]:
        b = db.q1("SELECT place_id FROM businesses WHERE id=?", (e["business_id"],))
        channel_id = "place:" + b["place_id"] if b else None
        db.update("businesses", e["business_id"], {"stage": "optout", "updated_at": db.iso()})
    db.ex("INSERT OR IGNORE INTO dnc (email, channel_id, reason, created_at) VALUES (?,?,?,?)",
          (e["to_email"].lower(), channel_id, "unsubscribe link", db.iso()))
    return e["to_email"]


def funnel() -> list[dict]:
    rows = db.q("SELECT niche, "
                "COUNT(*) scanned, "
                "SUM(teaser_job_id IS NOT NULL AND stage!='scanned') teaser_built, "
                "SUM(contact_email IS NOT NULL) contact_found, "
                "SUM(emails_sent>0) emailed, SUM(opened_at IS NOT NULL) opened, "
                "SUM(clicked_at IS NOT NULL) clicked, SUM(paid_at IS NOT NULL) paid, "
                "SUM(stage='unsubscribed') unsubscribed "
                "FROM prospects GROUP BY niche ORDER BY scanned DESC")
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ data retention
def purge_stale_api_data() -> dict:
    """YouTube Developer Policies limit how long API data may be stored without refresh.
    Unengaged prospects are deleted outright; engaged ones keep only our own outreach log."""
    cutoff = db.iso(db.now() - timedelta(days=config.API_DATA_RETENTION_DAYS))
    stale = db.q("SELECT * FROM prospects WHERE api_data_fetched_at<?", (cutoff,))
    deleted = scrubbed = 0
    for p in stale:
        if p["stage"] in ("scanned", "teaser_built", "contact_found", "skipped"):
            if p["teaser_job_id"]:
                db.ex("DELETE FROM audit_log WHERE job_id=?", (p["teaser_job_id"],))
                db.ex("DELETE FROM video_jobs WHERE job_id=?", (p["teaser_job_id"],))
                db.ex("DELETE FROM jobs WHERE id=?", (p["teaser_job_id"],))
            db.ex("DELETE FROM prospects WHERE id=?", (p["id"],))
            deleted += 1
        else:
            if p["teaser_job_id"]:
                db.ex("UPDATE audit_log SET metrics_json=NULL WHERE job_id=?", (p["teaser_job_id"],))
            db.update("prospects", p["id"], {"subscribers": None, "recent_median": None, "earlier_median": None,
                                             "last_upload_at": None, "api_data_fetched_at": None})
            scrubbed += 1
    return {"deleted": deleted, "scrubbed": scrubbed}
