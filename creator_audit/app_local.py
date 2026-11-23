"""Routes for the Local Business Scanner: self-serve free preview, check-up preview, mock site, opt-out,
website purchase (DIY or done-for-you) -> owner setup form -> live site (+ static copy on Netlify),
check-up PDF with a 30-day plan for buyers, and the admin pages."""
from __future__ import annotations

import json
import re

from fastapi import Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from markupsafe import escape

from . import config, db, local, outreach, sitepub
from .app import PRICES, admin, app, env, msg, page

_WEB_ORDER = "SELECT * FROM orders WHERE upload_token=? AND tier IN ('website','website_pro')"


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
    try:
        d = local.live_details(b)
    except Exception:  # noqa: BLE001  (Places cap reached or outage)
        d = None
    if not d:
        return None, None, msg("Not available", "We couldn't load this business's details right now. Try again later.", 503)
    return b, d, None


def _noindex(resp: HTMLResponse) -> HTMLResponse:
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


# ------------------------------------------------------------------ self-serve free preview
def client_ip(request: Request) -> str:
    # reelkix.com (Netlify) proxies to this server: Netlify passes the visitor's IP in its own header
    ip = request.headers.get("x-nf-client-connection-ip") or (request.headers.get("x-forwarded-for") or "").split(",")[0]
    return (ip.strip() or (request.client.host if request.client else "") or "unknown")[:64]


@app.get("/website-preview", response_class=HTMLResponse)
def self_serve_form(name: str = "", city: str = ""):
    return page("self_serve.html", f={"name": name, "city": city}, results=None, error=None)


@app.post("/website-preview/search", response_class=HTMLResponse)
def self_serve_search(request: Request, name: str = Form(""), city: str = Form("")):
    f = {"name": name, "city": city}
    if len(name.strip()) < 2 or len(city.strip()) < 2:
        return page("self_serve.html", 400, f=f, results=None, error="Enter your business name and your city.")
    try:
        results = local.self_serve_search(name, city, client_ip(request))
    except local.LookupLimited as e:
        return page("self_serve.html", 429, f=f, results=None, error=str(e))
    except Exception:  # noqa: BLE001  (Places outage or monthly free cap reached)
        return page("self_serve.html", 503, f=f, results=None, error="Google search isn't answering right now. Try again in a minute.")
    if not results:
        return page("self_serve.html", f=f, results=[], error=None)
    return page("self_serve.html", f=f, results=results, error=None)


@app.post("/website-preview/pick")
def self_serve_pick(request: Request, place_id: str = Form(...)):
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", place_id):
        raise HTTPException(400)
    try:
        tok = local.self_serve_build(place_id, client_ip(request))
    except local.LookupLimited as e:
        return page("self_serve.html", 429, f={"name": "", "city": ""}, results=None, error=str(e))
    return RedirectResponse(f"/b/{tok}", status_code=303)


@app.get("/b/{token}", response_class=HTMLResponse)
def business_preview(token: str):
    b, d, err = _live_business(token)
    if err:
        return err
    local.record_visit(b)
    ctx = local.preview_context(b, d)
    return _noindex(page("biz_preview.html", **ctx, gate=config.LOCAL_GATE, price=PRICES["website"],
                         pro_price=PRICES["website_pro"] if config.WEBSITE_PRO_ENABLED else None))


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
    html = env.get_template("site2.html").render(c=local.site_content(b, d), mock=True, preview_link=f"/b/{token}")
    return _noindex(HTMLResponse(html))


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
    order = db.q1(_WEB_ORDER, (tok,))
    if not order:
        return msg("Link not found", "This setup link isn't valid.", 404)
    site = db.q1("SELECT * FROM sites WHERE order_id=?", (order["id"],))
    if site and site["status"] == "live":
        return RedirectResponse(f"/s/{site['slug']}", status_code=303)
    pro = order["tier"] == "website_pro"
    f = {"owner_email": order["email"]}
    b = db.q1("SELECT * FROM businesses WHERE id=?", (order["business_id"],)) if order["business_id"] else None
    d = local.live_details(b) if b else None
    if d:  # prefill from Google + their current site once; from then on the owner's own answers are what we store
        c = local.site_content(b, d)
        f.update({"name": c["name"], "phone": c["phone"] or "", "address": c["address"] or "", "city": c["city"] or "",
                  "maps_uri": c["maps_uri"] or "", "tagline": c["headline"], "blurb": c["sub"],
                  "services": "\n".join(s["name"] for s in c["services"]), "hours": "\n".join(c["hours"] or [])})
        if c["email"]:
            f["email"] = c["email"]
    return page("site_setup.html", f=f, error=None, pro=pro, pdf=f"/bp/{tok}.pdf" if b else None)


@app.post("/site-setup/{tok}", response_class=HTMLResponse)
def site_setup_submit(tok: str, name: str = Form(...), tagline: str = Form(...), blurb: str = Form(...),
                      services: str = Form(...), phone: str = Form(""), email: str = Form(""), address: str = Form(""),
                      city: str = Form(""), maps_uri: str = Form(""), hours: str = Form(""), owner_email: str = Form(...),
                      domain: str = Form(""), notes: str = Form("")):
    order = db.q1(_WEB_ORDER, (tok,))
    if not order:
        return msg("Link not found", "This setup link isn't valid.", 404)
    site = db.q1("SELECT * FROM sites WHERE order_id=?", (order["id"],))
    if site["status"] == "live":
        return RedirectResponse(f"/s/{site['slug']}", status_code=303)
    pro = order["tier"] == "website_pro"
    form = {"name": name, "tagline": tagline, "blurb": blurb, "services": services, "phone": phone, "email": email,
            "address": address, "city": city, "maps_uri": maps_uri, "hours": hours, "owner_email": owner_email,
            "domain": domain, "notes": notes}
    if not outreach.EMAIL_RE.match(owner_email.strip()):
        return page("site_setup.html", 400, f=form, error="Enter a valid email for your confirmation.", pro=pro, pdf=None)
    if email.strip() and not outreach.EMAIL_RE.match(email.strip()):
        return page("site_setup.html", 400, f=form, error="The contact email doesn't look right.", pro=pro, pdf=None)
    domain = sitepub._clean_domain(domain)
    if domain and not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", domain):
        return page("site_setup.html", 400, f=form, error="The domain should look like yourbusiness.com", pro=pro, pdf=None)
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
               "type": (d or {}).get("type", ""), "requested_domain": domain or None,
               # snapshot for the premium template: their current site (logo, photos, colours, socials, about text)
               # and the Google rating at publish time
               "theme": local.theme_for((d or {}).get("type", ""), b["category"] if b else "")["key"],
               "site": json.loads(b["site_json"]) if b and b["site_json"] else None,
               "rating": (d or {}).get("rating"), "reviews": (d or {}).get("reviews") or 0,
               "type_label": (d or {}).get("type_label", ""), "category": b["category"] if b else "", "state": ((d or {}).get("postal") or {}).get("state", "")}
    db.update("sites", site["id"], {"slug": slug, "content_json": json.dumps(content), "status": "live",
                                    "category": content["type"], "live_at": db.iso()})
    db.update("orders", order["id"], {"token_used": 1, "email": owner_email.strip()})
    sitepub.publish_safely()
    from . import emailer
    link = config.link(f"/s/{slug}")
    pdf = config.link(f"/bp/{tok}.pdf") if b else None
    pdf_html = f'<p><b>Your check-up and 30-day plan (PDF):</b> <a href="{pdf}">{pdf}</a></p>' if pdf else ""
    pdf_text = f"\nYour check-up and 30-day plan (PDF): {pdf}" if pdf else ""
    if pro:
        dom_text = (f"We're now polishing the words and photos{', connecting ' + domain if domain else ''} and tidying your "
                    f"Google profile. You'll hear from us within {config.WEBSITE_PRO_DAYS} business days.")
        dom_html = f"<p>{dom_text}</p>"
    else:
        dom_html = (f"<p><b>Want your own domain?</b> At your domain registrar add a CNAME record for <code>www</code> pointing to "
                    f"<code>{config.SITE_DOMAIN_HINT}</code>, then reply with the domain and we'll connect it.</p>")
        dom_text = f"Own domain: CNAME www -> {config.SITE_DOMAIN_HINT}, then reply with the domain."
    try:
        emailer.send(
            "delivery", owner_email.strip(), f"Your website is live: {name}",
            f'<p>Your website is live: <a href="{link}">{link}</a></p>{pdf_html}'
            f"<p><b>Add it to Google:</b> put this link as the website on your Google Business Profile so customers find it.</p>"
            f"{dom_html}<p>Need changes to the text, services or hours? Just reply to this email.</p>",
            f"Your website is live: {link}{pdf_text}\n\nAdd it as the website on your Google Business Profile.\n{dom_text}",
            order_id=order["id"])
    except Exception:  # noqa: BLE001
        pass
    if pro or domain or notes.strip():
        emailer.alert(f"Website setup received ({'done-for-you' if pro else 'DIY'}): {name.strip()}",
                      f"Site: {link}\nOwner: {owner_email.strip()}\nDomain requested: {domain or '-'}\n"
                      f"Notes: {notes.strip()[:2000] or '-'}\nEdit it and set the domain: {config.link('/admin/local')}")
    return RedirectResponse(f"/s/{slug}", status_code=303)


@app.get("/bp/{tok}.pdf")
def business_pdf(tok: str):
    """Check-up + 30-day plan for a paid website order (re-fetches Google details; nothing from Google is stored)."""
    order = db.q1(_WEB_ORDER, (tok,))
    if not order or order["status"] == "refunded" or not order["business_id"]:
        raise HTTPException(404)
    b = db.q1("SELECT * FROM businesses WHERE id=?", (order["business_id"],))
    try:
        d = local.live_details(b) if b else None
    except Exception:  # noqa: BLE001
        d = None
    if not d:
        return msg("Not available right now", "We couldn't load your Google details. Try again in a few minutes.", 503)
    ctx = local.preview_context(b, d)
    site = db.q1("SELECT * FROM sites WHERE order_id=?", (order["id"],))
    url = config.link(f"/s/{site['slug']}") if site and site["status"] == "live" else None
    from . import bizpdf
    data = bizpdf.render(d["name"], d.get("address") or "", ctx["score"], ctx["insights"], local.plan_30(ctx["insights"]),
                         url, order["tier"] == "website_pro")
    fname = re.sub(r"[^A-Za-z0-9]+", "-", d["name"]).strip("-")[:40] or "check-up"
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fname}-30-day-plan.pdf"',
                             "X-Robots-Tag": "noindex, nofollow"})


@app.get("/s/{slug}", response_class=HTMLResponse)
def live_site(slug: str):
    site = db.q1("SELECT * FROM sites WHERE slug=? AND status='live'", (slug,))
    if not site:
        return msg("Not found", "No site here.", 404)
    return HTMLResponse(sitepub.site_html(site))


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
                page_next=len(rows) > 25)


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


@app.get("/admin/site/{sid}", response_class=HTMLResponse, dependencies=[Depends(admin)])
def admin_site_form(sid: int):
    site = db.q1("SELECT * FROM sites WHERE id=?", (sid,))
    if not site or site["status"] != "live":
        return msg("Not found", "Only live sites can be edited.", 404)
    c = json.loads(site["content_json"])
    f = {**c, "services": "\n".join(c.get("services") or []), "hours": "\n".join(c.get("hours") or []),
         "domain": site["domain"] or c.get("requested_domain") or ""}
    return page("admin_site.html", site=site, f=f)


@app.post("/admin/site/{sid}", response_class=HTMLResponse, dependencies=[Depends(admin)])
def admin_site_save(sid: int, name: str = Form(...), tagline: str = Form(...), blurb: str = Form(...),
                    services: str = Form(...), phone: str = Form(""), email: str = Form(""), address: str = Form(""),
                    city: str = Form(""), maps_uri: str = Form(""), hours: str = Form(""), domain: str = Form("")):
    site = db.q1("SELECT * FROM sites WHERE id=?", (sid,))
    if not site or site["status"] != "live":
        return msg("Not found", "Only live sites can be edited.", 404)
    c = json.loads(site["content_json"])
    c.update({"name": name.strip(), "tagline": tagline.strip(), "blurb": blurb.strip(),
              "services": [x.strip() for x in services.splitlines() if x.strip()][:12], "phone": phone.strip(),
              "email": email.strip() or None, "address": address.strip(), "city": city.strip(),
              "maps_uri": maps_uri.strip() if maps_uri.startswith("https://") else "",
              "hours": [x.strip() for x in hours.splitlines() if x.strip()]})
    dom = sitepub._clean_domain(domain) or None
    db.update("sites", sid, {"content_json": json.dumps(c), "domain": dom, "live_at": db.iso()})
    res = sitepub.publish_safely()
    note = ("Published to the fast static host." if "deploy" in res else
            f"Saved. Static publish: {escape(json.dumps(res))}")
    if dom:
        note += (f"<br>Domain: ask the owner to add a CNAME for <code>www.{escape(dom)}</code> pointing to "
                 f"<code>{escape(config.SITE_DOMAIN_HINT)}</code> (and forward the bare domain to www at their registrar). "
                 "HTTPS is issued automatically once DNS resolves.")
    return msg("Site saved", f'<a href="/s/{escape(site["slug"])}" target="_blank">View site</a><br>{note}')


@app.post("/admin/local/{bid}/social", dependencies=[Depends(admin)])
def admin_local_social(bid: int, facebook: str = Form(""), instagram: str = Form(""), source: str = Form("")):
    try:
        local.set_social(bid, facebook, instagram, source)
    except ValueError as e:
        return msg("Couldn't save social links", str(escape(str(e))), 400)
    return RedirectResponse("/admin/local", status_code=303)


@app.post("/admin/local/{bid}/skip", dependencies=[Depends(admin)])
def admin_local_skip(bid: int):
    db.ex("UPDATE businesses SET stage='skipped', updated_at=? WHERE id=? AND stage NOT IN ('paid','optout')",
          (db.iso(), bid))
    return RedirectResponse("/admin/local", status_code=303)
