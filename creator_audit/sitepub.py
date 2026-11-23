"""Publish every live customer site as static HTML to a separate free Netlify site, so paid sites load
instantly even while this (free, sleeping) server is asleep. reelkix.com proxies /s/* to that site, and
customer domains are mapped with host rules in its _redirects. Each deploy sends the full set of files
(Netlify's file-digest API only uploads the ones that changed)."""
from __future__ import annotations

import hashlib
import json
import logging
import re

import requests

from . import config, db

log = logging.getLogger("sitepub")
API = "https://api.netlify.com/api/v1"


def enabled() -> bool:
    return bool(config.NETLIFY_AUTH_TOKEN and config.NETLIFY_SITES_ID)


def site_content(site) -> dict:
    """Premium-template content for a paid site: the owner's confirmed answers win over everything else."""
    from . import copywriter, local
    s = json.loads(site["content_json"])
    owner = {"tagline": s.get("tagline"), "blurb": s.get("blurb"), "services": s.get("services"), "about": s.get("about")}
    return copywriter.build(
        name=s["name"], theme_key=s.get("theme") or local.theme_for(s.get("type", ""))["key"], city=s.get("city", ""),
        state=s.get("state", ""), phone=s.get("phone") or None, email=s.get("email"), address=s.get("address", ""),
        maps_uri=s.get("maps_uri") or None, hours=s.get("hours"), rating=s.get("rating"), reviews=s.get("reviews") or 0,
        site=s.get("site"), owner=owner, type_label=s.get("type_label", ""))


def site_html(site, c: dict | None = None) -> str:
    from .report import env
    return env.get_template("site2.html").render(c=c or site_content(site), mock=False, preview_link=None)


def _image(url: str) -> tuple[bytes, str] | None:
    """Their logo/photo, downloaded once and kept in the database, so the published site never depends on
    their old host. Returns (bytes, extension)."""
    key = "img:" + hashlib.sha1(url.encode()).hexdigest()
    cached = db.get_blob(key)
    ext = (re.search(r"\.(png|jpe?g|webp|gif|svg)(\?|$)", url.lower()) or [None, "jpg"])[1].replace("jpeg", "jpg")
    if cached:
        return cached, ext
    from . import sitecheck
    data = sitecheck.fetch_image(url)
    if not data:
        return None
    db.put_blob(key, data)
    return data, ext


def _localize(c: dict, slug: str, files: dict[str, bytes]) -> dict:
    """Swap remote image URLs for copies served next to the page."""
    def one(url):
        if not url or not url.startswith("http"):
            return url
        got = _image(url)
        if not got:
            return None
        name = f"img/{hashlib.sha1(url.encode()).hexdigest()[:12]}.{got[1]}"
        files[f"/s/{slug}/{name}"] = got[0]
        return name
    c = dict(c)
    c["logo"] = one(c.get("logo"))
    c["photos"] = [p for p in (one(u) for u in c.get("photos") or []) if p]
    c["hero_photo"] = one(c.get("hero_photo")) if c.get("hero_photo") not in (None, "") else None
    return c


def _clean_domain(d: str | None) -> str:
    d = (d or "").strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0]
    return d.removeprefix("www.")


def build_files() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    rules = []
    for site in db.q("SELECT * FROM sites WHERE status='live' AND slug IS NOT NULL ORDER BY id"):
        c = _localize(site_content(site), site["slug"], files)
        files[f"/s/{site['slug']}/index.html"] = site_html(site, c).encode("utf-8")
        dom = _clean_domain(site["domain"])
        if dom:
            for host in (dom, f"www.{dom}"):
                rules.append(f"https://{host}/*  /s/{site['slug']}/:splat  200!")
    files["/index.html"] = (f'<!doctype html><meta charset="utf-8"><title>{config.BRAND}</title>'
                            f'<meta http-equiv="refresh" content="0;url={config.SITE_URL}/">').encode()
    files["/_redirects"] = ("\n".join(rules + ["/*  /index.html  404"]) + "\n").encode()
    return files


def publish() -> dict:
    """Deploy all live sites. Returns a small summary; raises on API errors."""
    if not enabled():
        return {"skipped": "NETLIFY_AUTH_TOKEN / NETLIFY_SITES_ID not set"}
    h = {"Authorization": f"Bearer {config.NETLIFY_AUTH_TOKEN}"}
    files = build_files()
    digests = {path: hashlib.sha1(body).hexdigest() for path, body in files.items()}
    r = requests.post(f"{API}/sites/{config.NETLIFY_SITES_ID}/deploys", headers=h, json={"files": digests}, timeout=30)
    r.raise_for_status()
    dep = r.json()
    need = set(dep.get("required") or [])
    for path, body in files.items():
        if digests[path] in need:
            u = requests.put(f"{API}/deploys/{dep['id']}/files{path}", data=body, timeout=60,
                             headers={**h, "Content-Type": "application/octet-stream"})
            u.raise_for_status()
    domains = sorted({x for s in db.q("SELECT domain FROM sites WHERE status='live'") if (d := _clean_domain(s["domain"]))
                      for x in (d, f"www.{d}")})
    if domains:  # let Netlify answer (and issue HTTPS) for customer domains pointed at it
        requests.patch(f"{API}/sites/{config.NETLIFY_SITES_ID}", headers=h, json={"domain_aliases": domains},
                       timeout=30).raise_for_status()
    db.ex("UPDATE sites SET published_at=? WHERE status='live'", (db.iso(),))
    return {"deploy": dep["id"], "files": len(files), "uploaded": len(need), "domains": len(domains)}


def publish_safely() -> dict:
    try:
        return publish()
    except Exception as e:  # noqa: BLE001  (never block a customer flow on the static copy)
        log.exception("site publish failed")
        return {"error": str(e)[:200]}


def publish_pending() -> dict:
    """Scheduler task: retry when a live site changed after the last successful publish."""
    if not enabled():
        return {"skipped": "not configured"}
    if not db.q1("SELECT 1 FROM sites WHERE status='live' AND (published_at IS NULL OR published_at < live_at)"):
        return {"pending": 0}
    return publish_safely()
