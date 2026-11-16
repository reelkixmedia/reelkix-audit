"""API server + pages: checkout, token upload, status, report, teaser, tracking, admin."""
from __future__ import annotations

import base64
import json
import secrets
import urllib.parse
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from markupsafe import escape

from . import config, csv_parser, db, local, outreach, tasks
from .report import env, render_html

app = FastAPI(title=config.PRODUCT_NAME, docs_url=None, redoc_url=None)
security = HTTPBasic()
PIXEL = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
PRICES = {"standard": config.PRICE_STANDARD, "premium": config.PRICE_PREMIUM, "unlock": config.PRICE_UNLOCK,
          "website": config.PRICE_WEBSITE}
TIER_NAMES = {"standard": "Standard audit", "premium": "Premium plan", "unlock": "Full public-data audit",
              "website": "Website"}


@app.on_event("startup")
def _startup():
    db.init()


def page(name: str, status: int = 200, **ctx) -> HTMLResponse:
    return HTMLResponse(env.get_template(name).render(prices=PRICES, **ctx), status_code=status)


def msg(title: str, body: str, status: int = 200) -> HTMLResponse:
    return page("message.html", status, title=title, body=body)


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


env.filters["money"] = money


# ------------------------------------------------------------------ public pages
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/audit")


@app.get("/audit", response_class=HTMLResponse)
def landing(tier: str = "", ref: str = ""):
    return page("landing.html", tier=tier, ref=ref)


@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    return page("privacy.html")


@app.get("/terms", response_class=HTMLResponse)
def terms():
    return page("terms.html")


# ------------------------------------------------------------------ checkout
def _teaser_by_token(tok: str):
    return db.q1("SELECT * FROM prospects WHERE teaser_token=?", (tok,)) if tok else None


@app.post("/checkout")
def checkout(tier: str = Form(...), ref: str = Form(""), email: str = Form("")):
    if tier not in PRICES or (tier == "unlock" and not (config.UNLOCK_ENABLED and _teaser_by_token(ref))):
        raise HTTPException(400, "Unknown product")
    if tier == "website":
        b = local.get(ref)
        if not b or config.LOCAL_GATE != "paywall" or b["stage"] in ("paid", "optout"):
            raise HTTPException(400, "Unknown product")
    if config.DEV_CHECKOUT:
        sid = "dev_" + db.token(12)
        dest = fulfill(sid, tier, email or None, ref)
        return RedirectResponse(dest, status_code=303)
    import stripe
    stripe.api_key = config.STRIPE_SECRET_KEY
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{"quantity": 1, "price_data": {"currency": "usd", "unit_amount": PRICES[tier],
                                                   "product_data": {"name": f"{config.PRODUCT_NAME} — {TIER_NAMES[tier]}"}}}],
        success_url=f"{config.BASE_URL}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{config.BASE_URL}/audit",
        customer_email=email or None,
        metadata={"tier": tier, "ref": ref},
        custom_text={"submit": {"message": "Refunds: if your audit can't be built from your data we refund in full. "
                                           "Otherwise email us within 14 days for a refund. Audits are advice, not a guarantee of growth."}},
    )
    return RedirectResponse(session.url, status_code=303)


def fulfill(session_id: str, tier: str, email: str | None, ref: str) -> str:
    """Create the order once per paid session. Returns where to send the buyer."""
    existing = db.q1("SELECT * FROM orders WHERE stripe_session_id=?", (session_id,))
    if existing:
        return _after_payment(existing)
    if tier == "website":
        return _fulfill_website(session_id, email, ref)
    teaser = _teaser_by_token(ref)
    oid = db.insert("orders", {"stripe_session_id": session_id, "tier": tier, "price_cents": PRICES[tier],
                               "email": email, "upload_token": db.token(), "teaser_id": teaser["id"] if teaser else None,
                               "created_at": db.iso()})
    if teaser:
        outreach.advance(teaser["id"], "paid", "paid_at")
    order = db.q1("SELECT * FROM orders WHERE id=?", (oid,))
    if tier == "unlock":
        db.insert("jobs", {"kind": "unlock", "tier": "unlock", "order_id": oid, "teaser_id": teaser["id"],
                           "channel_id": teaser["channel_id"], "channel_title": teaser["channel_title"], "email": email,
                           "status": "queued", "report_token": db.token(), "created_at": db.iso()})
        db.update("orders", oid, {"token_used": 1})
    elif email:
        from . import emailer
        link = f"{config.BASE_URL}/upload/{order['upload_token']}"
        try:
            emailer.send("upload_link", email, "Next step: upload your YouTube Studio export",
                         f'<p>Thanks for your order. Upload your channel data here (link works once, no account needed):</p>'
                         f'<p><a href="{link}">{link}</a></p>',
                         f"Thanks for your order. Upload your channel data here: {link}", order_id=oid)
        except Exception:  # noqa: BLE001
            pass
    return _after_payment(db.q1("SELECT * FROM orders WHERE id=?", (oid,)))


def _fulfill_website(session_id: str, email: str | None, ref: str) -> str:
    b = local.get(ref)
    oid = db.insert("orders", {"stripe_session_id": session_id, "tier": "website", "price_cents": PRICES["website"],
                               "email": email, "upload_token": db.token(), "business_id": b["id"] if b else None,
                               "created_at": db.iso()})
    if b:
        db.update("businesses", b["id"], {"stage": "paid", "paid_at": db.iso(), "expires_at": None, "updated_at": db.iso()})
    db.insert("sites", {"business_id": b["id"] if b else None, "order_id": oid, "status": "setup", "created_at": db.iso()})
    order = db.q1("SELECT * FROM orders WHERE id=?", (oid,))
    if email:
        from . import emailer
        link = f"{config.BASE_URL}/site-setup/{order['upload_token']}"
        try:
            emailer.send("upload_link", email, "Next step: confirm your website details",
                         f'<p>Thanks! Confirm your details and your site goes live right away:</p><p><a href="{link}">{link}</a></p>',
                         f"Thanks! Confirm your details and your site goes live right away: {link}", order_id=oid)
        except Exception:  # noqa: BLE001
            pass
    return _after_payment(order)


def _after_payment(order) -> str:
    if order["tier"] == "website":
        site = db.q1("SELECT * FROM sites WHERE order_id=?", (order["id"],))
        return f"/s/{site['slug']}" if site and site["status"] == "live" else f"/site-setup/{order['upload_token']}"
    if order["tier"] == "unlock":
        job = db.q1("SELECT report_token FROM jobs WHERE order_id=?", (order["id"],))
        return f"/status/{job['report_token']}"
    if order["token_used"]:
        job = db.q1("SELECT report_token FROM jobs WHERE order_id=?", (order["id"],))
        return f"/status/{job['report_token']}" if job else "/audit"
    return f"/upload/{order['upload_token']}"


@app.get("/checkout/success")
def checkout_success(session_id: str):
    order = db.q1("SELECT * FROM orders WHERE stripe_session_id=?", (session_id,))
    if order:
        return RedirectResponse(_after_payment(order), status_code=303)
    import stripe
    stripe.api_key = config.STRIPE_SECRET_KEY
    s = stripe.checkout.Session.retrieve(session_id)
    if s.payment_status != "paid":
        return msg("Payment pending", "We haven't received confirmation from Stripe yet. Refresh in a minute.")
    md = s.metadata or {}
    email = (s.customer_details or {}).get("email") if isinstance(s.customer_details, dict) else getattr(s.customer_details, "email", None)
    return RedirectResponse(fulfill(s.id, md.get("tier", "standard"), email, md.get("ref", "")), status_code=303)


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    import stripe
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, config.STRIPE_WEBHOOK_SECRET)
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "bad signature")
    obj = event["data"]["object"]
    if event["type"] == "checkout.session.completed" and obj.get("payment_status") == "paid":
        md = obj.get("metadata") or {}
        email = (obj.get("customer_details") or {}).get("email")
        fulfill(obj["id"], md.get("tier", "standard"), email, md.get("ref", ""))
    elif event["type"] == "charge.refunded":
        pi = obj.get("payment_intent")
        if pi:
            sessions = stripe.checkout.Session.list(payment_intent=pi, limit=1)
            for s in sessions.data:
                db.ex("UPDATE orders SET status='refunded' WHERE stripe_session_id=?", (s.id,))
    return {"ok": True}


# ------------------------------------------------------------------ upload
@app.get("/upload/{tok}", response_class=HTMLResponse)
def upload_form(tok: str):
    order = db.q1("SELECT * FROM orders WHERE upload_token=?", (tok,))
    if not order:
        return msg("Link not found", "This upload link isn't valid. Check the email we sent you.", 404)
    if order["token_used"]:
        return RedirectResponse(_after_payment(order), status_code=303)
    return page("upload.html", order=order, error=None, form={})


@app.post("/upload/{tok}", response_class=HTMLResponse)
async def upload_submit(tok: str, channel_url: str = Form(...), email: str = Form(""), file: UploadFile = File(...),
                        niche: str = Form(""), capacity: str = Form(""), goal: str = Form(""), notes: str = Form("")):
    order = db.q1("SELECT * FROM orders WHERE upload_token=?", (tok,))
    if not order:
        return msg("Link not found", "This upload link isn't valid.", 404)
    if order["token_used"]:
        return RedirectResponse(_after_payment(order), status_code=303)
    form = {"channel_url": channel_url, "email": email, "niche": niche, "capacity": capacity, "goal": goal, "notes": notes}
    email = (email or order["email"] or "").strip()

    def fail(err):
        return page("upload.html", 400, order=order, error=err, form=form)

    cu = channel_url.strip()
    if not ("youtube.com/" in cu or cu.startswith("@") or cu.startswith("UC")):
        return fail("Paste your channel link, like https://www.youtube.com/@yourname")
    if not outreach.EMAIL_RE.match(email):
        return fail("Enter the email address where we should send your report.")
    if order["tier"] == "premium" and not (niche.strip() and capacity.strip() and goal.strip()):
        return fail("Fill in the goals form so we can build your plan.")
    raw = await file.read()
    if len(raw) > 20 * 1024 * 1024:
        return fail("That file is over 20 MB — it doesn't look like a Studio export.")
    try:
        parsed = csv_parser.parse(file.filename or "", raw)
    except csv_parser.UploadError as e:
        return fail(str(e))

    path = config.UPLOAD_DIR / f"{order['id']}_{secrets.token_hex(6)}{Path(file.filename or 'x.csv').suffix or '.csv'}"
    path.write_bytes(raw)
    goals = json.dumps({"niche": niche, "posting_capacity": capacity, "goal": goal, "notes": notes}) if order["tier"] == "premium" else None
    rtok = db.token()
    db.insert("jobs", {"kind": "audit", "tier": order["tier"], "order_id": order["id"], "channel_url": cu,
                       "email": email, "csv_path": str(path), "goals_json": goals, "status": "queued",
                       "report_token": rtok, "created_at": db.iso()})
    db.update("orders", order["id"], {"token_used": 1, "email": email})
    return RedirectResponse(f"/status/{rtok}?w={len(parsed.warnings)}", status_code=303)


# ------------------------------------------------------------------ status + report
@app.get("/status/{rtok}", response_class=HTMLResponse)
def status(rtok: str):
    job = db.q1("SELECT * FROM jobs WHERE report_token=?", (rtok,))
    if not job:
        return msg("Not found", "We couldn't find that audit.", 404)
    if job["status"] == "done":
        return RedirectResponse(f"/r/{rtok}", status_code=303)
    ahead = db.q1("SELECT COUNT(*) n FROM jobs WHERE status IN ('queued','running') AND id<?", (job["id"],))["n"]
    return page("status.html", job=job, ahead=ahead)


def _load_audit(job) -> dict | None:
    a = db.q1("SELECT * FROM audit_log WHERE job_id=? ORDER BY id DESC LIMIT 1", (job["id"],))
    if not a or not a["metrics_json"]:
        return None
    from . import scoring
    m = json.loads(a["metrics_json"])
    return {"metrics": m, "subscores": json.loads(a["subscores_json"]), "score": int(a["score"]),
            "narrative": json.loads(a["narrative_json"]), "problems": scoring.problems(m, json.loads(a["subscores_json"])),
            "created_at": a["created_at"]}


def _video_for(job_id: int):
    v = db.q1("SELECT * FROM video_jobs WHERE job_id=? AND status='done' ORDER BY id DESC LIMIT 1", (job_id,))
    return f"/v/{v['id']}.mp4" if v else None


@app.get("/r/{rtok}", response_class=HTMLResponse)
def report_page(rtok: str):
    job = db.q1("SELECT * FROM jobs WHERE report_token=? AND kind!='teaser'", (rtok,))
    if not job or job["status"] != "done":
        return RedirectResponse(f"/status/{rtok}") if job else msg("Not found", "We couldn't find that report.", 404)
    audit = _load_audit(job)
    if not audit:
        return msg("Report removed", "This report's data was deleted.", 410)
    return HTMLResponse(render_html(dict(job), audit, video=_video_for(job["id"]), pdf=f"/r/{rtok}/pdf",
                                    prices=PRICES, teaser=False))


@app.get("/r/{rtok}/pdf")
def report_pdf(rtok: str):
    p = config.REPORT_DIR / f"{rtok}.pdf"
    if not p.exists() or not db.q1("SELECT 1 FROM jobs WHERE report_token=?", (rtok,)):
        raise HTTPException(404)
    return FileResponse(p, media_type="application/pdf", filename="channel-audit.pdf")


@app.get("/t/{tok}", response_class=HTMLResponse)
def teaser_page(tok: str):
    p = _teaser_by_token(tok)
    if not p:
        return msg("Not found", "This teaser link isn't valid.", 404)
    exp = db.parse_iso(p["teaser_expires_at"])
    job = db.q1("SELECT * FROM jobs WHERE id=?", (p["teaser_job_id"],)) if p["teaser_job_id"] else None
    audit = _load_audit(job) if job and job["status"] == "done" else None
    if not audit or (exp and exp < db.now()):
        return msg("This teaser has expired", f"Free teasers are kept for {config.TEASER_EXPIRY_DAYS} days. "
                   f"<a href='/audit'>Get a full audit</a> any time.", 410)
    resp = HTMLResponse(render_html(dict(job), audit, video=_video_for(job["id"]), teaser=True, teaser_token=tok,
                                    prices=PRICES, unlock_enabled=config.UNLOCK_ENABLED))
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@app.get("/v/{vid}.mp4")
def video(vid: int):
    v = db.q1("SELECT * FROM video_jobs WHERE id=? AND status='done'", (vid,))
    if not v or not v["output_path"] or not Path(v["output_path"]).exists():
        raise HTTPException(404)
    return FileResponse(v["output_path"], media_type="video/mp4")


# ------------------------------------------------------------------ tracking + unsubscribe
@app.get("/o/{tok}.gif")
def open_pixel(tok: str):
    e = db.q1("SELECT * FROM emails WHERE token=?", (tok,))
    if e:
        db.ex("UPDATE emails SET opened_at=COALESCE(opened_at, ?) WHERE id=?", (db.iso(), e["id"]))
        if e["prospect_id"]:
            outreach.advance(e["prospect_id"], "opened", "opened_at")
    return Response(PIXEL, media_type="image/gif", headers={"Cache-Control": "no-store"})


def _safe_target(u: str) -> str:
    host = urllib.parse.urlparse(u).netloc.lower()
    ok_hosts = {urllib.parse.urlparse(config.BASE_URL).netloc.lower(), "reelkix.com", "www.reelkix.com", "app.reelkix.com"}
    return u if host in ok_hosts else config.BASE_URL


@app.get("/c/{tok}")
def click(tok: str, u: str):
    e = db.q1("SELECT * FROM emails WHERE token=?", (tok,))
    if e:
        db.ex("UPDATE emails SET clicked_at=COALESCE(clicked_at, ?), opened_at=COALESCE(opened_at, ?) WHERE id=?",
              (db.iso(), db.iso(), e["id"]))
        if e["prospect_id"]:
            outreach.advance(e["prospect_id"], "clicked", "clicked_at")
            db.ex("UPDATE prospects SET opened_at=COALESCE(opened_at, ?) WHERE id=?", (db.iso(), e["prospect_id"]))
    return RedirectResponse(_safe_target(u), status_code=302)


@app.api_route("/u/{tok}", methods=["GET", "POST"], response_class=HTMLResponse)
def unsubscribe(tok: str):
    email = outreach.unsubscribe(tok)
    if not email:
        return msg("Unsubscribe", "That link isn't valid, but you can email us and we'll remove you.", 404)
    return msg("You're unsubscribed", f"{escape(email)} won't get any more emails from {config.BRAND}.")


# ------------------------------------------------------------------ data deletion
@app.get("/delete-my-data", response_class=HTMLResponse)
def delete_form():
    return page("delete.html", done=None)


@app.post("/delete-my-data", response_class=HTMLResponse)
def delete_submit(email: str = Form(""), report_link: str = Form("")):
    rtok = report_link.rstrip("/").split("/")[-1] if report_link else ""
    jobs = []
    if rtok:
        jobs = db.q("SELECT * FROM jobs WHERE report_token=?", (rtok,))
    if not jobs:
        return page("delete.html", 400, done=False)
    for j in jobs:
        _purge_job(j)
        for other in db.q("SELECT * FROM jobs WHERE lower(email)=lower(?)", (j["email"] or "",)):
            _purge_job(other)
    db.insert("deletion_requests", {"email": email or (jobs[0]["email"] if jobs else None), "report_token": rtok,
                                    "created_at": db.iso()})
    return page("delete.html", done=True)


def _purge_job(j) -> None:
    db.ex("DELETE FROM audit_log WHERE job_id=?", (j["id"],))
    if j["csv_path"]:
        Path(j["csv_path"]).unlink(missing_ok=True)
    if j["report_token"]:
        (config.REPORT_DIR / f"{j['report_token']}.pdf").unlink(missing_ok=True)
    for v in db.q("SELECT output_path FROM video_jobs WHERE job_id=?", (j["id"],)):
        if v["output_path"]:
            Path(v["output_path"]).unlink(missing_ok=True)
    db.ex("DELETE FROM video_jobs WHERE job_id=?", (j["id"],))
    db.ex("UPDATE jobs SET channel_url=NULL, csv_path=NULL, goals_json=NULL, email=NULL WHERE id=?", (j["id"],))


# ------------------------------------------------------------------ mascot render worker API
def _worker(x_worker_key: str = Header("")):
    if not config.VIDEO_WORKER_KEY or not secrets.compare_digest(x_worker_key, config.VIDEO_WORKER_KEY):
        raise HTTPException(401)


@app.post("/api/video-jobs/claim", dependencies=[Depends(_worker)])
def video_claim():
    row = db.q1("UPDATE video_jobs SET status='claimed', claimed_at=? WHERE id=(SELECT id FROM video_jobs "
                "WHERE status='pending' ORDER BY kind='walkthrough' DESC, id LIMIT 1) RETURNING *", (db.iso(),))
    db.conn().commit()
    return dict(row) if row else {}


@app.post("/api/video-jobs/{vid}/done", dependencies=[Depends(_worker)])
async def video_done(vid: int, file: UploadFile = File(...)):
    out = config.VIDEO_DIR / f"{vid}.mp4"
    out.write_bytes(await file.read())
    db.update("video_jobs", vid, {"status": "done", "output_path": str(out)})
    return {"ok": True}


# ------------------------------------------------------------------ admin
def admin(creds: HTTPBasicCredentials = Depends(security)):
    ok = secrets.compare_digest(creds.username, config.ADMIN_USER) and secrets.compare_digest(creds.password, config.ADMIN_PASSWORD)
    if not ok:
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})


@app.get("/admin", response_class=HTMLResponse, dependencies=[Depends(admin)])
def admin_home():
    jobs = db.q("SELECT * FROM jobs WHERE kind!='teaser' ORDER BY id DESC LIMIT 50")
    flagged = db.q("SELECT * FROM jobs WHERE status IN ('flagged','failed') ORDER BY id DESC LIMIT 50")
    tokens = db.q1("SELECT COUNT(*) n, AVG(input_tokens) ti, AVG(output_tokens) tout FROM audit_log WHERE model!='template'")
    return page("admin.html", stats=tasks.weekly_stats(), lifetime=tasks.weekly_stats(36500), jobs=jobs, flagged=flagged,
                grades=tasks.advice_grades(), tokens=tokens, cfg=config)


@app.post("/admin/jobs/{jid}/retry", dependencies=[Depends(admin)])
def admin_retry(jid: int):
    db.update("jobs", jid, {"status": "queued", "attempts": 0, "error": None})
    return RedirectResponse("/admin", status_code=303)


@app.get("/admin/queue", response_class=HTMLResponse, dependencies=[Depends(admin)])
def admin_queue(stage: str = "review"):
    stages = {"review": "('teaser_built','contact_found')", "approved": "('approved')",
              "emailed": "('emailed','opened','clicked')", "paid": "('paid')", "scanned": "('scanned')",
              "skipped": "('skipped','unsubscribed')"}
    where = stages.get(stage, stages["review"])
    rows = db.q(f"SELECT p.*, a.narrative_json FROM prospects p LEFT JOIN audit_log a ON a.job_id=p.teaser_job_id "
                f"WHERE p.stage IN {where} ORDER BY p.updated_at DESC LIMIT 200")
    items = []
    for r in rows:
        d = dict(r)
        nar = json.loads(r["narrative_json"]) if r["narrative_json"] else {}
        d["problem"] = (nar.get("problems") or [{}])[0].get("title")
        items.append(d)
    counts = {k: db.q1(f"SELECT COUNT(*) n FROM prospects WHERE stage IN {v}")["n"] for k, v in stages.items()}
    dnc = db.q("SELECT * FROM dnc ORDER BY id DESC LIMIT 100")
    return page("admin_queue.html", items=items, stage=stage, counts=counts, funnel=outreach.funnel(), dnc=dnc, cfg=config)


@app.post("/admin/prospects/{pid}/contact", dependencies=[Depends(admin)])
def admin_contact(pid: int, email: str = Form(...), source: str = Form(...), approve: str = Form("")):
    try:
        outreach.set_contact(pid, email, source, approve=bool(approve))
    except ValueError as e:
        return msg("Couldn't save contact", str(e), 400)
    return RedirectResponse("/admin/queue", status_code=303)


@app.post("/admin/prospects/{pid}/approve", dependencies=[Depends(admin)])
def admin_approve(pid: int):
    outreach.approve(pid)
    return RedirectResponse("/admin/queue", status_code=303)


@app.post("/admin/prospects/{pid}/skip", dependencies=[Depends(admin)])
def admin_skip(pid: int):
    outreach.skip(pid)
    return RedirectResponse("/admin/queue", status_code=303)


@app.post("/admin/import", response_class=HTMLResponse, dependencies=[Depends(admin)])
async def admin_import(file: UploadFile = File(...), source: str = Form("lead source import")):
    res = outreach.import_leads(await file.read(), source)
    return msg("Import finished", f"Added {res['added']}, updated {res['updated']}.<br>"
               + "<br>".join(str(escape(e)) for e in res["errors"][:50]))


@app.post("/admin/add-channel", dependencies=[Depends(admin)])
def admin_add_channel(channel: str = Form(...), niche: str = Form("manual")):
    try:
        outreach.add_channel(channel, niche)
    except Exception as e:  # noqa: BLE001
        return msg("Couldn't add channel", str(e), 400)
    return RedirectResponse("/admin/queue?stage=scanned", status_code=303)


@app.post("/admin/scan", response_class=HTMLResponse, dependencies=[Depends(admin)])
def admin_scan(searches: int = Form(5)):
    return msg("Scan finished", json.dumps(outreach.daily_scan(searches)))


@app.post("/admin/send", response_class=HTMLResponse, dependencies=[Depends(admin)])
def admin_send():
    return msg("Send run finished", json.dumps(outreach.send_due(ignore_hours=True)))


@app.post("/admin/dnc", dependencies=[Depends(admin)])
def admin_dnc(email: str = Form(""), channel_id: str = Form(""), reason: str = Form("manual")):
    if email or channel_id:
        db.ex("INSERT OR IGNORE INTO dnc (email, channel_id, reason, created_at) VALUES (?,?,?,?)",
              (email.lower() or None, channel_id or None, reason, db.iso()))
        if channel_id:
            db.ex("UPDATE prospects SET stage='unsubscribed' WHERE channel_id=?", (channel_id,))
        if email:
            db.ex("UPDATE prospects SET stage='unsubscribed' WHERE lower(contact_email)=lower(?)", (email,))
    return RedirectResponse("/admin/queue?stage=skipped", status_code=303)


from . import app_local  # noqa: E402,F401  (registers local-business routes)
