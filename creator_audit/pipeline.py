"""The worker: metrics -> score -> Claude narrative -> number check -> report -> delivery.

It is the only part that calls Claude, and it never sends anything that failed the check.
"""
from __future__ import annotations

import json
import logging
import time
import traceback
from datetime import timedelta
from pathlib import Path

from . import config, csv_parser, db, emailer, metrics, narrative, numcheck, report, scoring, youtube

log = logging.getLogger("worker")

SKIP_KEYS = {"video_id", "new_title", "day", "format", "key"}
PUBLIC_TIERS = {"teaser", "unlock"}


class Flagged(Exception):
    pass


def _load_data(job: dict):
    yt = youtube.client()
    channel = {"id": job.get("channel_id"), "title": job.get("channel_title"), "subscribers": None}
    api_videos = None
    if yt:
        cid = job.get("channel_id") or yt.resolve_channel(job["channel_url"])
        channel, api_videos = yt.channel_with_videos(cid)
    elif job["kind"] != "audit":
        raise RuntimeError("Public-data audits need YOUTUBE_API_KEY")
    csv_df = None
    if job.get("csv_path") and job["tier"] not in PUBLIC_TIERS:
        raw = db.get_blob(job["csv_path"])
        if raw is None:
            raise csv_parser.UploadError("The uploaded file is no longer stored. Ask the customer to upload it again.")
        csv_df = csv_parser.parse(job["csv_path"].rsplit("/", 1)[-1], raw).df
    return channel, metrics.merge_sources(csv_df, api_videos)


def _write_checked(tier, m, subs, score, probs, goals):
    """Generate, check, regenerate failing sections once; flag on a second failure."""
    nums, title_nums = numcheck.allowed_values(m, subs, score, [p["evidence"] for p in probs])
    nar = narrative.generate(tier, m, subs, score, probs, goals)
    tokens_in, tokens_out = nar.input_tokens, nar.output_tokens
    failures = 0
    for attempt in range(2):
        bad = {k: numcheck.check_section(v, nums, title_nums, SKIP_KEYS) for k, v in nar.data.items()}
        bad = {k: v for k, v in bad.items() if v}
        if not bad:
            break
        failures += 1
        log.warning("number check failed (attempt %s): %s", attempt + 1, bad)
        if attempt == 1:
            raise Flagged(f"Number check failed twice: {json.dumps(bad)}")
        redo = narrative.generate(tier, m, subs, score, probs, goals,
                                  feedback=[n for v in bad.values() for n in v])
        tokens_in += redo.input_tokens
        tokens_out += redo.output_tokens
        for k in bad:  # regenerate only the failing sections
            if k in redo.data:
                nar.data[k] = redo.data[k]
    # problems must reference real candidates, in our order; fix_type comes from code
    by_key = {p["key"]: p for p in probs}
    clean = []
    for p in nar.data.get("problems", []):
        if p.get("key") in by_key:
            p["fix_type"] = by_key[p["key"]]["fix_type"]
            clean.append(p)
    nar.data["problems"] = clean
    return nar, tokens_in, tokens_out, failures


def run_job(job: dict) -> None:
    tier = job["tier"]
    channel, df = _load_data(job)
    m = metrics.compute(df, channel)
    subs = scoring.subscores(m)
    score = scoring.overall(subs)
    probs = scoring.problems(m, subs)
    goals = json.loads(job["goals_json"]) if job.get("goals_json") else None

    nar, tin, tout, fails = _write_checked(tier, m, subs, score, probs, goals)

    audit_id = db.insert("audit_log", {
        "job_id": job["id"], "channel_id": channel.get("id"), "tier": tier, "metrics_json": db.dumps(m),
        "score": score, "subscores_json": db.dumps(subs), "narrative_json": db.dumps(nar.data),
        "report_version": config.REPORT_VERSION, "prompt_version": config.PROMPT_VERSION, "model": nar.model,
        "input_tokens": tin, "output_tokens": tout, "numcheck_failures": fails, "created_at": db.iso()})
    for p in nar.data["problems"]:
        db.insert("advice", {"job_id": job["id"], "channel_id": channel.get("id"), "fix_type": p["fix_type"],
                             "title": p["title"], "created_at": db.iso()})

    rtoken = job.get("report_token") or db.token()
    db.update("jobs", job["id"], {"channel_id": channel.get("id"), "channel_title": channel.get("title"),
                                  "report_token": rtoken})
    job = dict(db.q1("SELECT * FROM jobs WHERE id=?", (job["id"],)))
    audit = {"metrics": m, "subscores": subs, "score": score, "narrative": nar.data, "problems": probs}
    pdf_path = None
    if tier != "teaser":
        pdf_path = report.render_pdf(job, audit, config.REPORT_DIR / f"{rtoken}.pdf")

    # privacy: uploaded CSV is deleted once the report is built; only metrics JSON is kept
    if job.get("csv_path"):
        db.del_blob(job["csv_path"])
        db.update("jobs", job["id"], {"csv_path": None})

    # mascot video jobs (rendered by a separate worker — see /api/video-jobs)
    if tier in ("teaser", "premium"):
        db.insert("video_jobs", {"job_id": job["id"], "kind": "teaser_clip" if tier == "teaser" else "walkthrough",
                                 "script": nar.data.get("mascot_script", ""), "created_at": db.iso()})

    if tier == "teaser":
        db.ex("UPDATE prospects SET stage=CASE WHEN stage='scanned' THEN 'teaser_built' ELSE stage END, "
              "teaser_score=?, teaser_job_id=?, updated_at=? WHERE id=?",
              (score, job["id"], db.iso(), job["teaser_id"]))
        return

    _deliver(job, audit, pdf_path)
    now = db.now()
    base_vpu = m["momentum"]["recent_median_views"]  # compared with median views of uploads after the audit
    base_upw = m["cadence"]["uploads_per_week"]
    for d in (30, 60, 90):
        db.ex("INSERT OR IGNORE INTO outcome_checks (job_id, channel_id, day_offset, due_at, baseline_vpu, baseline_upw) "
              "VALUES (?,?,?,?,?,?)", (job["id"], channel.get("id"), d, db.iso(now + timedelta(days=d)), base_vpu, base_upw))
    for d in (30, 60):
        db.ex("INSERT OR IGNORE INTO followups (job_id, day_offset, due_at) VALUES (?,?,?)",
              (job["id"], d, db.iso(now + timedelta(days=d))))


UPSELL = {
    "unlock": ("Want the numbers YouTube only shows you?",
               "The Standard audit adds your click-through, retention and subscriber data from YouTube Studio.",
               "/audit?tier=standard"),
    "standard": ("Want a plan, not just a diagnosis?",
                 "Premium turns this audit into a 30-day content schedule built around your goals, with title and thumbnail rewrites.",
                 "/audit?tier=premium"),
    "premium": ("Want someone to do the editing?",
                f"{config.BRAND} edits videos and manages channels for creators. Reply to this email to talk.",
                f"{config.SITE_URL}/editing.html"),
}


def _deliver(job: dict, audit: dict, pdf_path: Path | None) -> None:
    if not job.get("email"):
        return
    link = config.link(f"/r/{job['report_token']}")
    # until Premium is for sale, Standard buyers get the editing offer instead
    h, body, path = UPSELL["premium" if job["tier"] == "standard" and not config.PREMIUM_ENABLED else job["tier"]]
    up = path if path.startswith("http") else config.BASE_URL + path
    title = audit["metrics"]["channel"].get("title") or "your channel"
    first = audit["narrative"]["problems"][0]["title"] if audit["narrative"]["problems"] else ""
    html = (f"<p>Your {config.PRODUCT_NAME} for <b>{title}</b> is ready.</p>"
            f"<p style='font-size:28px;margin:8px 0'><b>{audit['score']}</b>/100</p>"
            f"<p>Biggest issue: {first}.</p>"
            f"<p><a href='{link}' style='display:inline-block;background:#1e5ac8;color:#fff;padding:12px 20px;"
            f"border-radius:8px;text-decoration:none'>Open your report</a></p>"
            f"<p>The PDF is attached too.</p><p><b>{h}</b><br>{body} <a href='{up}'>See how</a></p>")
    text = (f"Your {config.PRODUCT_NAME} for {title} is ready.\nScore: {audit['score']}/100\n"
            f"Biggest issue: {first}\n\nOpen your report: {link}\n\n{h}\n{body} {up}")
    emailer.send("delivery", job["email"], f"Your channel audit is ready: {audit['score']}/100",
                 html.replace("'", '"'), text, attachments=[pdf_path] if pdf_path else None,
                 job_id=job["id"], order_id=job.get("order_id"))


# ------------------------------------------------------------------ loop
def claim() -> dict | None:
    row = db.q1("UPDATE jobs SET status='running', started_at=?, attempts=attempts+1 "
                "WHERE id=(SELECT id FROM jobs WHERE status='queued' ORDER BY id LIMIT 1) AND status='queued' RETURNING *",
                (db.iso(),))
    db.conn().commit()
    return dict(row) if row else None


def process_one() -> bool:
    job = claim()
    if not job:
        return False
    log.info("job %s (%s/%s) started", job["id"], job["kind"], job["tier"])
    try:
        run_job(job)
        db.update("jobs", job["id"], {"status": "done", "finished_at": db.iso(), "error": None})
        log.info("job %s done", job["id"])
    except Flagged as e:
        db.update("jobs", job["id"], {"status": "flagged", "error": str(e)[:2000], "finished_at": db.iso()})
        emailer.alert(f"Job {job['id']} flagged for review", str(e))
    except Exception as e:  # noqa: BLE001
        err = f"{e}\n{traceback.format_exc()}"[-3000:]
        status = "queued" if job["attempts"] < 3 and not isinstance(e, csv_parser.UploadError) else "failed"
        db.update("jobs", job["id"], {"status": status, "error": err})
        log.exception("job %s error -> %s", job["id"], status)
        if status == "failed":
            emailer.alert(f"Job {job['id']} failed", err)
    return True


def worker_loop(poll: float = 3.0) -> None:
    db.init()
    while True:
        if not process_one():
            time.sleep(poll)
