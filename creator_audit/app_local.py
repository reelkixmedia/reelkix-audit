"""Routes for the Local Business Scanner: check-up preview, mock site, QR, opt-out,
$200 website purchase -> owner setup form -> live site, and the admin page."""
from __future__ import annotations

import io
import json
import re

from fastapi import Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from markupsafe import escape

from . import config, db, local, outreach
from .app import PRICES, admin, app, env, msg, page


def _site_ctx_from_details(d: dict, theme: dict) -> dict:
    return {"name": d["name"], "phone": d.get("phone"), "address": d.get("address"), "city": d["postal"].get("city"),
            "maps_uri": d.get("maps_uri"), "hours": d.get("hours"), "services": theme["services"],
            "tagline": theme["tagline"], "blurb": theme["blurb"], "type_label": d.get("type_label"), "email": None}


def _live_business(token: str):
    b = local.get(token)
    if not b:
        return None, None, msg("Not found", "This preview link isn't valid.", 404)
    if local.is_expired(b):
        return None, None, msg("This preview has expired",
                               f"Previews are kept for {config.MOCK_EXPIRY_DAYS} days. Email {config.SUPPORT_EMAIL} "
                               "if you'd still like a website.", 410)
    d = local.live_details(b)
    if not d:
        return None, None, msg("Not available", "We couldn't load this business's details right now.", 503)
    return b, d, None


def _noindex(resp: HTMLResponse) -> HTMLResponse:
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@app.get("/b/{token}", response_class=HTMLResponse)
def business_preview(token: str):
    b, d, err = _live_business(token)
    if err:
        return err
    local.record_visit(b)
    ctx = local.preview_context(b, d)
    return _noindex(page("biz_preview.html", **ctx, gate=config.LOCAL_GATE, price=PRICES["website"]))


@app.get("/b/{token}/consult")
def business_consult(token: str):
    b = local.get(token)
    if not b:
        raise HTTPException(404)
    db.ex("UPDATE businesses SET consult_clicked_at=COALESCE(consult_clicked_at, ?), updated_at=?, "
          "stage=CASE WHEN stage NOT IN ('paid','optout') THEN 'consult' ELSE stage END WHERE id=?",
          (db.iso(), db.iso(), b["id"]))
    return RedirectResponse(config.CONSULT_URL or f"mailto:{config.SUPPORT_EMAIL}", status_code=302)


@app.get("/m/{token}", response_class=HTMLResponse)
def mock_site(token: str):
    b, d, err = _live_business(token)
    if err:
        return err
    theme = local.theme_for(d.get("type", ""))
    html = env.get_template("site.html").render(s=_site_ctx_from_details(d, theme), t=theme, mock=True,
                                                preview_link=f"/b/{token}")
    return _noindex(HTMLResponse(html))


@app.get("/qr/{token}.png")
def qr(token: str):
    import segno
    if not local.get(token):
        raise HTTPException(404)
    buf = io.BytesIO()
    segno.make(f"{config.BASE_URL}/b/{token}", error="m").save(buf, kind="png", scale=10, border=2)
    return Response(buf.getvalue(), media_type="image/png")


@app.get("/optout/{token}", response_class=HTMLResponse)
def optout_form(token: str):
    if not local.get(token):
        return msg("Not found", f"That link isn't valid. Email {config.SUPPORT_EMAIL} and we'll remove you.", 404)
    return _noindex(page("optout.html", done=False))


@app.post("/optout/{token}", response_class=HTMLResponse)
def optout_submit(token: str):
    b = local.get(token)
    if b and b["stage"] != "optout":
        local.optout(b)
    return page("optout.html", done=True)


# ------------------------------------------------------------------ purchase -> setup -> live
@app.get("/site-setup/{tok}", response_class=HTMLResponse)
def site_setup_form(tok: str):
    order = db.q1("SELECT * FROM orders WHERE upload_token=? AND tier='website'", (tok,))
    if not order:
        return msg("Link not found", "This setup link isn't valid.", 404)
    site = db.q1("SELECT * FROM sites WHERE order_id=?", (order["id"],))
    if site and site["status"] == "live":
        return RedirectResponse(f"/s/{site['slug']}", status_code=303)
    f = {"owner_email": order["email"]}
    b = db.q1("SELECT * FROM businesses WHERE id=?", (order["business_id"],)) if order["business_id"] else None
    d = local.live_details(b) if b else None
    if d:  # prefill from Google once; from then on the owner's own answers are what we store
        s = _site_ctx_from_details(d, local.theme_for(d.get("type", "")))
        f.update({k: s[k] or "" for k in ("name", "phone", "address", "city", "maps_uri", "tagline", "blurb")})
        f["services"] = "\n".join(s["services"])
        f["hours"] = "\n".join(s["hours"] or [])
    return page("site_setup.html", f=f, error=None)


@app.post("/site-setup/{tok}", response_class=HTMLResponse)
def site_setup_submit(tok: str, name: str = Form(...), tagline: str = Form(...), blurb: str = Form(...),
                      services: str = Form(...), phone: str = Form(""), email: str = Form(""), address: str = Form(""),
                      city: str = Form(""), maps_uri: str = Form(""), hours: str = Form(""), owner_email: str = Form(...)):
    order = db.q1("SELECT * FROM orders WHERE upload_token=? AND tier='website'", (tok,))
    if not order:
        return msg("Link not found", "This setup link isn't valid.", 404)
    site = db.q1("SELECT * FROM sites WHERE order_id=?", (order["id"],))
    if site["status"] == "live":
        return RedirectResponse(f"/s/{site['slug']}", status_code=303)
    form = {"name": name, "tagline": tagline, "blurb": blurb, "services": services, "phone": phone, "email": email,
            "address": address, "city": city, "maps_uri": maps_uri, "hours": hours, "owner_email": owner_email}
    if not outreach.EMAIL_RE.match(owner_email.strip()):
        return page("site_setup.html", 400, f=form, error="Enter a valid email for your confirmation.")
    if email.strip() and not outreach.EMAIL_RE.match(email.strip()):
        return page("site_setup.html", 400, f=form, error="The contact email doesn't look right.")
    if maps_uri and not maps_uri.startswith("https://"):
        maps_uri = ""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "site"
    slug, n = base, 1
    while db.q1("SELECT 1 FROM sites WHERE slug=?", (slug,)):
        n += 1
        slug = f"{base}-{n}"
    b = db.q1("SELECT * FROM businesses WHERE id=?", (order["business_id"],)) if order["business_id"] else None
    d = local.live_details(b) if b else None
    content = {"name": name.strip(), "tagline": tagline.strip(), "blurb": blurb.strip(),
               "services": [s.strip() for s in services.splitlines() if s.strip()][:12], "phone": phone.strip(),
               "email": email.strip() or None, "address": address.strip(), "city": city.strip(),
               "maps_uri": maps_uri.strip(), "hours": [h.strip() for h in hours.splitlines() if h.strip()],
               "type": (d or {}).get("type", "")}
    db.update("sites", site["id"], {"slug": slug, "content_json": json.dumps(content), "status": "live",
                                    "category": content["type"], "live_at": db.iso()})
    db.update("orders", order["id"], {"token_used": 1, "email": owner_email.strip()})
    from . import emailer
    link = f"{config.BASE_URL}/s/{slug}"
    try:
        emailer.send(
            "delivery", owner_email.strip(), f"Your website is live: {name}",
            f'<p>Your website is live: <a href="{link}">{link}</a></p>'
            f"<p><b>Add it to Google:</b> put this link as the website on your Google Business Profile so customers find it.</p>"
            f"<p><b>Want your own domain?</b> At your domain registrar add a CNAME record for <code>www</code> pointing to "
            f"<code>{config.SITE_DOMAIN_HINT}</code>, then reply with the domain and we'll connect it.</p>"
            f"<p>Need changes to the text, services or hours? Just reply to this email.</p>",
            f"Your website is live: {link}\n\nAdd it as the website on your Google Business Profile.\n"
            f"Own domain: CNAME www -> {config.SITE_DOMAIN_HINT}, then reply with the domain.",
            order_id=order["id"])
    except Exception:  # noqa: BLE001
        pass
    return RedirectResponse(f"/s/{slug}", status_code=303)


@app.get("/s/{slug}", response_class=HTMLResponse)
def live_site(slug: str):
    site = db.q1("SELECT * FROM sites WHERE slug=? AND status='live'", (slug,))
    if not site:
        return msg("Not found", "No site here.", 404)
    s = json.loads(site["content_json"])
    return HTMLResponse(env.get_template("site.html").render(s=s, t=local.theme_for(s.get("type", "")), mock=False,
                                                             preview_link=None))


# ------------------------------------------------------------------ admin
STAGES = {"new": "('built','contact_found','approved')", "contacted": "('contacted')",
          "visited": "('visited','consult')", "paid": "('paid')", "closed": "('skipped','optout','expired')"}


@app.get("/admin/local", response_class=HTMLResponse, dependencies=[Depends(admin)])
def admin_local(stage: str = "new", page_no: int = 0):
    where = STAGES.get(stage, STAGES["new"])
    rows = db.q(f"SELECT * FROM businesses WHERE stage IN {where} ORDER BY updated_at DESC LIMIT 26 OFFSET ?",
                (page_no * 25,))
    items = [{"b": b, "d": local.live_details(b)} for b in rows[:25]]
    counts = {k: db.q1(f"SELECT COUNT(*) n FROM businesses WHERE stage IN {v}")["n"] for k, v in STAGES.items()}
    return page("admin_local.html", rows=items, stage=stage, counts=counts, funnel=local.funnel(),
                sites=db.q("SELECT * FROM sites ORDER BY id DESC LIMIT 50"), page=page_no,
                page_next=len(rows) > 25, spent=local._postcard_spend_this_month())


@app.post("/admin/local/scan", response_class=HTMLResponse, dependencies=[Depends(admin)])
def admin_local_scan(searches: int = Form(3)):
    return msg("Local scan finished", str(escape(json.dumps(local.daily_scan(searches)))))


@app.post("/admin/local/send", response_class=HTMLResponse, dependencies=[Depends(admin)])
def admin_local_send():
    return msg("Send finished", str(escape(json.dumps(local.send_due(ignore_hours=True)))))


@app.post("/admin/local/import", response_class=HTMLResponse, dependencies=[Depends(admin)])
async def admin_local_import(file: UploadFile = File(...), source: str = Form("lead source import")):
    res = local.import_leads(await file.read(), source)
    return msg("Import finished", f"Updated {res['updated']}.<br>" + "<br>".join(str(escape(e)) for e in res["errors"][:50]))


@app.post("/admin/local/{bid}/contact", dependencies=[Depends(admin)])
def admin_local_contact(bid: int, email: str = Form(...), source: str = Form(...), approve: str = Form("")):
    try:
        local.set_contact(bid, email, source, approve=bool(approve))
    except ValueError as e:
        return msg("Couldn't save contact", str(escape(str(e))), 400)
    return RedirectResponse("/admin/local", status_code=303)


@app.post("/admin/local/{bid}/skip", dependencies=[Depends(admin)])
def admin_local_skip(bid: int):
    db.ex("UPDATE businesses SET stage='skipped', updated_at=? WHERE id=? AND stage NOT IN ('paid','optout')",
          (db.iso(), bid))
    return RedirectResponse("/admin/local", status_code=303)
