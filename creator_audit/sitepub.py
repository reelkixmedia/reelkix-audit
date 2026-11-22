"""Publish every live customer site as static HTML to a separate free Netlify site, so paid sites load
instantly even while this (free, sleeping) server is asleep. reelkix.com proxies /s/* to that site, and
customer domains are mapped with host rules in its _redirects. Each deploy sends the full set of files
(Netlify's file-digest API only uploads the ones that changed)."""
from __future__ import annotations

import hashlib
import json
import logging

import requests

from . import config, db

log = logging.getLogger("sitepub")
API = "https://api.netlify.com/api/v1"


def enabled() -> bool:
    return bool(config.NETLIFY_AUTH_TOKEN and config.NETLIFY_SITES_ID)


def site_html(site) -> str:
    from . import local
    from .report import env
    s = json.loads(site["content_json"])
    return env.get_template("site.html").render(s=s, t=local.theme_for(s.get("type", "")), mock=False, preview_link=None)


def _clean_domain(d: str | None) -> str:
    d = (d or "").strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0]
    return d.removeprefix("www.")


def build_files() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    rules = []
    for site in db.q("SELECT * FROM sites WHERE status='live' AND slug IS NOT NULL ORDER BY id"):
        files[f"/s/{site['slug']}/index.html"] = site_html(site).encode("utf-8")
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
