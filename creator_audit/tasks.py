"""Scheduled tasks: scanner, outreach sender, follow-ups, outcome checks, weekly summary, purges."""
from __future__ import annotations

import json
import logging
import statistics
import time
from datetime import timedelta

from . import config, db, emailer, local, outreach, youtube

log = logging.getLogger("scheduler")


def send_followups() -> int:
    """30/60-day re-audit invitations to paying customers."""
    n = 0
    for f in db.q("SELECT f.*, j.email, j.channel_title, j.tier FROM followups f JOIN jobs j ON j.id=f.job_id "
                  "WHERE f.sent_at IS NULL AND f.due_at<=?", (db.iso(),)):
        if not f["email"] or db.q1("SELECT 1 FROM deletion_requests WHERE lower(email)=lower(?)", (f["email"],)):
            db.update("followups", f["id"], {"sent_at": db.iso()})
            continue
        link = f"{config.BASE_URL}/audit?tier={f['tier'] if f['tier'] in ('standard', 'premium') else 'standard'}&ref=reaudit{f['job_id']}"
        title = f["channel_title"] or "your channel"
        html = (f"<p>It's been {f['day_offset']} days since your audit of <b>{title}</b>.</p>"
                f"<p>Run a fresh audit to see what changed since you started on the fixes — your new score is "
                f"compared side by side with the old one.</p><p><a href='{link}'>Re-audit my channel</a></p>").replace("'", '"')
        text = f"It's been {f['day_offset']} days since your audit of {title}. Re-audit to see what changed: {link}"
        try:
            emailer.send("followup", f["email"], f"{f['day_offset']} days later: see what changed on {title}",
                         html, text, job_id=f["job_id"])
            db.update("followups", f["id"], {"sent_at": db.iso()})
            n += 1
        except Exception as e:  # noqa: BLE001
            log.error("followup %s failed: %s", f["id"], e)
    return n


def run_outcome_checks() -> int:
    yt = youtube.client()
    if not yt:
        return 0
    n = 0
    for oc in db.q("SELECT o.*, j.finished_at FROM outcome_checks o JOIN jobs j ON j.id=o.job_id "
                   "WHERE o.done_at IS NULL AND o.due_at<=? AND o.channel_id IS NOT NULL", (db.iso(),)):
        try:
            _, vids = yt.channel_with_videos(oc["channel_id"])
        except Exception as e:  # noqa: BLE001
            log.error("outcome check %s: %s", oc["id"], e)
            continue
        since = db.parse_iso(oc["finished_at"]) or (db.now() - timedelta(days=oc["day_offset"]))
        new = [v for v in vids if db.parse_iso(v["published"].replace("Z", "+00:00")) >= since]
        weeks = max((db.now() - since).days / 7, 1)
        vpu = statistics.median(v["views_public"] for v in new) if new else 0.0
        db.update("outcome_checks", oc["id"], {"done_at": db.iso(), "vpu": round(vpu, 1), "upw": round(len(new) / weeks, 2)})
        n += 1
    return n


def advice_grades() -> list[dict]:
    """Which kinds of advice come before growth: per fix_type, latest outcome vs baseline."""
    rows = db.q("""
        SELECT a.fix_type, o.baseline_vpu, o.vpu, o.baseline_upw, o.upw
        FROM advice a
        JOIN outcome_checks o ON o.job_id=a.job_id
        WHERE o.done_at IS NOT NULL AND o.day_offset=(SELECT MAX(day_offset) FROM outcome_checks o2
                                                     WHERE o2.job_id=a.job_id AND o2.done_at IS NOT NULL)""")
    agg: dict[str, list] = {}
    for r in rows:
        if r["baseline_vpu"]:
            agg.setdefault(r["fix_type"], []).append((r["vpu"] - r["baseline_vpu"]) / r["baseline_vpu"] * 100)
    return [{"fix_type": k, "audits": len(v), "median_vpu_change_pct": round(statistics.median(v), 1),
             "grew_pct": round(sum(x > 0 for x in v) / len(v) * 100, 1)} for k, v in sorted(agg.items())]


def weekly_stats(days: int = 7) -> dict:
    since = db.iso(db.now() - timedelta(days=days))
    sales = db.q("SELECT tier, COUNT(*) n, SUM(price_cents) cents, SUM(status='refunded') refunds FROM orders "
                 "WHERE created_at>=? GROUP BY tier", (since,))
    jobs = db.q1("SELECT SUM(status='done') done, SUM(status='failed') failed, SUM(status='flagged') flagged "
                 "FROM jobs WHERE kind!='teaser' AND created_at>=?", (since,))
    out = db.q1("SELECT SUM(kind='outreach') sent, SUM(kind='outreach' AND opened_at IS NOT NULL) opened, "
                "SUM(kind='outreach' AND clicked_at IS NOT NULL) clicked FROM emails WHERE created_at>=?", (since,))
    paid = db.q1("SELECT COUNT(*) n FROM prospects WHERE paid_at>=?", (since,))["n"]
    revenue = sum((r["cents"] or 0) for r in sales if True) - sum(
        (r["price_cents"] or 0) for r in db.q("SELECT price_cents FROM orders WHERE status='refunded' AND created_at>=?", (since,)))
    return {"sales": [dict(r) for r in sales], "revenue_cents": revenue, "jobs": dict(jobs) if jobs else {},
            "outreach": {**(dict(out) if out else {}), "paid": paid}, "funnel": outreach.funnel()}


def weekly_summary() -> None:
    if not config.ADMIN_EMAIL:
        return
    s = weekly_stats()
    lines = [f"Week ending {db.now():%Y-%m-%d}", "",
             f"Revenue (net of refunds): ${s['revenue_cents'] / 100:,.2f}"]
    for r in s["sales"]:
        lines.append(f"  {r['tier']}: {r['n']} sold, {r['refunds'] or 0} refunded")
    j = s["jobs"]
    lines += ["", f"Audits delivered: {j.get('done') or 0}, failed: {j.get('failed') or 0}, flagged: {j.get('flagged') or 0}"]
    o = s["outreach"]
    lines += ["", f"Outreach: {o.get('sent') or 0} sent, {o.get('opened') or 0} opened, {o.get('clicked') or 0} clicked, "
                  f"{o.get('paid') or 0} paid", "", "Funnel by niche:"]
    for f in s["funnel"]:
        lines.append("  " + json.dumps(f))
    lines += ["", "Local businesses (category/city):"]
    for f in local.funnel():
        lines.append("  " + json.dumps(f))
    sites = db.q1("SELECT COUNT(*) n FROM sites WHERE status='live' AND live_at>=?",
                  (db.iso(db.now() - timedelta(days=7)),))["n"]
    lines.append(f"Websites published this week: {sites}")
    text = "\n".join(lines)
    emailer.send("weekly", config.ADMIN_EMAIL, f"[{config.PRODUCT_NAME}] Weekly summary", f"<pre>{text}</pre>", text)


# ------------------------------------------------------------------ scheduler loop
def _due(name: str, every: timedelta, at_hour: int | None = None, weekday: int | None = None) -> bool:
    row = db.q1("SELECT last_run_at FROM task_runs WHERE name=?", (name,))
    last = db.parse_iso(row["last_run_at"]) if row else None
    now = db.now().astimezone()
    if at_hour is not None and now.hour < at_hour:
        return False
    if weekday is not None and now.weekday() != weekday:
        return False
    if last and at_hour is not None:
        return last.astimezone().date() < now.date()
    return last is None or db.now() - last >= every


def _mark(name: str) -> None:
    db.ex("INSERT INTO task_runs (name, last_run_at) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET last_run_at=excluded.last_run_at",
          (name, db.iso()))


TASKS = [
    ("daily_scan", timedelta(days=1), config.SCANNER_HOUR, None, outreach.daily_scan),
    ("outreach_send", timedelta(minutes=15), None, None, outreach.send_due),
    ("followups", timedelta(hours=1), None, None, send_followups),
    ("outcome_checks", timedelta(hours=6), None, None, run_outcome_checks),
    ("purge_api_data", timedelta(days=1), 3, None, outreach.purge_stale_api_data),
    ("weekly_summary", timedelta(days=6), 8, config.WEEKLY_SUMMARY_WEEKDAY, weekly_summary),
    ("local_scan", timedelta(days=1), config.LOCAL_SCAN_HOUR, None, local.daily_scan),
    ("local_send", timedelta(minutes=15), None, None, local.send_due),
    ("local_expire", timedelta(days=1), 3, None, local.expire_and_purge),
]


def tick() -> None:
    for name, every, hour, wd, fn in TASKS:
        if _due(name, every, hour, wd):
            try:
                res = fn()
                log.info("%s -> %s", name, res)
            except Exception:  # noqa: BLE001
                log.exception("task %s failed", name)
            _mark(name)


def scheduler_loop(poll: float = 60.0) -> None:
    db.init()
    while True:
        tick()
        time.sleep(poll)
