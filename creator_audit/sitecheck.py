"""Read a business's OWN website (homepage + contact/about/services pages), score it the way a visitor
experiences it, and pull out what a redesign needs: their words, services, logo, photos, brand colour,
the email and social profiles they publish.

Rules: only http(s) to public hosts, robots.txt is respected, small page budget (max 4 pages, 2 MB each,
short timeouts), a clear User-Agent that says who we are. Nothing from Google is used here.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
import time
from collections import Counter
from html.parser import HTMLParser
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests

from . import config, db

log = logging.getLogger("sitecheck")
UA = f"Mozilla/5.0 (compatible; {config.BRAND}SiteCheck/1.0; +{config.SITE_URL}/website-preview)"
MAX_BYTES = 2_000_000
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}")
SOCIAL_HOSTS = {"facebook": ("facebook.com", "fb.com"), "instagram": ("instagram.com",), "tiktok": ("tiktok.com",),
                "youtube": ("youtube.com", "youtu.be"), "yelp": ("yelp.com",), "linkedin": ("linkedin.com",),
                "x": ("twitter.com", "x.com")}
BAD_EMAIL_PARTS = ("example.", "sentry", "wixpress", "domain.com", "email.com", "yourdomain", "@2x", ".png", ".jpg",
                   ".gif", ".webp", ".svg", "godaddy.com", "squarespace", "noreply", "no-reply", "user@")
NAV_WORDS = {"home", "about", "about us", "contact", "contact us", "services", "gallery", "menu", "blog", "faq",
             "reviews", "testimonials", "login", "cart", "shop", "book now", "careers", "privacy policy", "terms",
             "sitemap", "search", "skip to content", "read more", "learn more", "call now", "get directions"}


PARKED = re.compile(r"domain (is|may be) for sale|buy this domain|this domain (is|has been) (parked|registered)|"
                    r"hugedomains|\bsedo\b|\bdan\.com\b|domain parking|parked free|account suspended|website expired", re.I)
BOILERPLATE = re.compile(r"cookie|privacy|©|all rights reserved|javascript|accessib|ada compliance|screen reader|"
                         r"newsletter|subscribe|sign up for|browser|terms of (use|service)|click here|disclaimer|"
                         r"powered by|this site (uses|is protected)|recaptcha|gift card balance", re.I)


def _root(host: str | None) -> str:
    parts = (host or "").lower().removeprefix("www.").split(".")
    return ".".join(parts[-2:])


class FetchError(RuntimeError):
    pass


def _public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def fetch(url: str, timeout: float = 12.0) -> tuple[str, str, float, int]:
    """GET a public http(s) page. Returns (final_url, html, seconds, bytes)."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname or not _public_host(u.hostname):
        raise FetchError("not a public http(s) address")
    t0 = time.monotonic()
    with requests.get(url, headers={"User-Agent": UA, "Accept": "text/html,*/*;q=0.5"}, timeout=timeout,
                      stream=True, allow_redirects=True) as r:
        final = r.url
        fu = urlparse(final)
        if fu.scheme not in ("http", "https") or not fu.hostname or not _public_host(fu.hostname):
            raise FetchError("redirected to a non-public address")
        if r.status_code >= 400:
            raise FetchError(f"HTTP {r.status_code}")
        if "html" not in r.headers.get("content-type", "text/html"):
            raise FetchError("not an HTML page")
        body = b""
        for chunk in r.iter_content(65536):
            body += chunk
            if len(body) > MAX_BYTES:
                break
        enc = r.encoding if r.encoding and r.encoding.lower() != "iso-8859-1" else "utf-8"
    return final, body.decode(enc, errors="replace"), time.monotonic() - t0, len(body)


def fetch_image(url: str, max_bytes: int = 4_000_000) -> bytes | None:
    """Download one public image (their logo/photo) for a site they bought. None if it isn't an image."""
    if config.GOOGLE_PLACES_API_KEY.lower() == "fake" and "example.com" in url:
        return b"\x89PNG\r\n\x1a\n" + b"0" * 64
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname or not _public_host(u.hostname):
        return None
    try:
        with requests.get(url, headers={"User-Agent": UA}, timeout=15, stream=True) as r:
            if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
                return None
            body = b""
            for chunk in r.iter_content(65536):
                body += chunk
                if len(body) > max_bytes:
                    return None
            return body
    except requests.RequestException:
        return None


def allowed(url: str) -> bool:
    u = urlparse(url)
    rp = robotparser.RobotFileParser()
    try:
        _, txt, _, _ = fetch(f"{u.scheme}://{u.netloc}/robots.txt", timeout=6)
        rp.parse(txt.splitlines())
    except Exception:  # noqa: BLE001  (no robots.txt = allowed)
        return True
    return rp.can_fetch(UA, url)


# ------------------------------------------------------------------ HTML parsing
class _Page(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template"}

    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.title = ""
        self.meta: dict[str, str] = {}
        self.links: list[tuple[str, str]] = []  # (href, text)
        self.imgs: list[dict] = []
        self.icons: list[str] = []
        self.h1: list[str] = []
        self.h2: list[str] = []
        self.paras: list[str] = []
        self.items: list[str] = []
        self.text: list[str] = []
        self.colors: Counter = Counter()
        self.legacy = Counter()
        self.buttons = 0
        self._stack: list[str] = []
        self._buf: list[str] = []
        self._link: str | None = None
        self._link_text: list[str] = []
        self._in_nav = 0
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag in self.SKIP:
            self._skip += 1
        if tag in ("nav", "header", "footer"):
            self._in_nav += 1
        if tag in ("font", "center", "marquee", "frameset", "frame"):
            self.legacy[tag] += 1
        if tag == "table" and not a.get("role"):
            self.legacy["table"] += 1
        if tag in ("object", "embed") and "flash" in (a.get("type", "") + a.get("src", "")).lower():
            self.legacy["flash"] += 1
        if tag == "meta":
            k = (a.get("name") or a.get("property") or "").lower()
            if k:
                self.meta[k] = a.get("content", "")
        if tag == "link" and "icon" in a.get("rel", "").lower() and a.get("href"):
            self.icons.append(urljoin(self.base, a["href"]))
        if tag == "a":
            self._link = a.get("href", "")
            self._link_text = []
            cls = (a.get("class", "") + " " + a.get("id", "")).lower()
            if "btn" in cls or "button" in cls:
                self.buttons += 1
        if tag == "button":
            self.buttons += 1
        if tag == "img":
            src = a.get("src") or a.get("data-src") or a.get("data-lazy-src") or ""
            if not src and a.get("srcset"):
                src = a["srcset"].split(",")[-1].strip().split(" ")[0]
            if src and not src.startswith("data:"):
                self.imgs.append({"src": urljoin(self.base, src), "alt": a.get("alt", ""),
                                  "w": _int(a.get("width")), "h": _int(a.get("height")),
                                  "hint": (a.get("class", "") + " " + a.get("id", "") + " " + src).lower(),
                                  "nav": self._in_nav > 0})
        style = a.get("style", "")
        for c in re.findall(r"#[0-9a-fA-F]{6}\b", style):
            self.colors[c.lower()] += 1
        self._stack.append(tag)
        if tag in ("h1", "h2", "p", "li", "title"):
            self._buf = []

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag in ("nav", "header", "footer") and self._in_nav:
            self._in_nav -= 1
        if tag == "a" and self._link is not None:
            self.links.append((self._link, " ".join(self._link_text).strip()))
            self._link = None
        text = _clean(" ".join(self._buf))
        if tag == "title":
            self.title = text
        elif tag == "h1" and text:
            self.h1.append(text)
        elif tag == "h2" and text:
            self.h2.append(text)
        elif tag == "p" and len(text) >= 40:
            self.paras.append(text)
        elif tag == "li" and 3 <= len(text) <= 60 and not self._in_nav:
            self.items.append(text)
        if self._stack:
            self._stack.pop()

    def handle_data(self, data):
        if self._skip:
            if self._stack and self._stack[-1] == "style":
                for c in re.findall(r"#[0-9a-fA-F]{6}\b", data):
                    self.colors[c.lower()] += 1
            return
        self._buf.append(data)
        self.text.append(data)
        if self._link is not None:
            self._link_text.append(data)


def _int(v) -> int | None:
    try:
        return int(str(v).replace("px", ""))
    except (TypeError, ValueError):
        return None


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _parse(url: str, html: str) -> _Page:
    p = _Page(url)
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001  (broken HTML still gives partial results)
        pass
    return p


# ------------------------------------------------------------------ extraction helpers
def _emails(pages: list[_Page], domain: str) -> list[str]:
    found = []
    for p in pages:
        for href, _ in p.links:
            if href.lower().startswith("mailto:"):
                found.append(href[7:].split("?")[0])
        txt = " ".join(p.text)
        txt = re.sub(r"\s*[\[(]\s*at\s*[\])]\s*", "@", txt, flags=re.I)
        txt = re.sub(r"\s*[\[(]\s*dot\s*[\])]\s*", ".", txt, flags=re.I)
        found += EMAIL_RE.findall(txt)
    out = []
    for e in found:
        e = e.strip().strip(".").lower()
        if EMAIL_RE.fullmatch(e) and not any(b in e for b in BAD_EMAIL_PARTS) and e not in out:
            out.append(e)
    base = domain.removeprefix("www.")
    out.sort(key=lambda e: (not e.endswith("@" + base), e.split("@")[0] not in ("info", "contact", "hello", "office", "service")))
    return out[:3]


def _socials(pages: list[_Page]) -> dict:
    out = {}
    for p in pages:
        for href, _ in p.links:
            u = urlparse(href if "://" in href else "https://" + href.lstrip("/"))
            host = (u.hostname or "").lower().removeprefix("www.").removeprefix("m.")
            for k, hosts in SOCIAL_HOSTS.items():
                if host in hosts and u.path.strip("/") and k not in out:
                    path = u.path.strip("/")
                    if not any(x in path.lower() for x in ("sharer", "share", "intent", "plugins", "dialog")):
                        out[k] = f"https://www.{hosts[0]}/{path}"
    return out


def _phones(pages: list[_Page]) -> list[str]:
    out = []
    for p in pages:
        for href, _ in p.links:
            if href.lower().startswith("tel:"):
                n = re.sub(r"[^\d+]", "", href[4:])
                if len(n) >= 10 and n not in out:
                    out.append(n)
    return out


def _photos(p: _Page, logo: str | None) -> list[str]:
    out = []
    for i in p.imgs:
        src, hint = i["src"], i["hint"]
        if src == logo or i["nav"] or any(x in hint for x in ("logo", "icon", "sprite", "badge", "avatar", "pixel",
                                                               "tracking", "spinner", "loader", "placeholder", "emoji",
                                                               "banner", "promo", "offer", "deal", "price", "coupon",
                                                               "special", "sale", "flyer", "menu-board", "/ads/", "qr")):
            continue  # graphics with baked-in text crop badly and date fast; we want real photos
        if not re.search(r"\.(jpe?g|webp|png)(\?|$)", src.lower()) and "image" not in src.lower():
            continue
        if (i["w"] and i["w"] < 300) or (i["h"] and i["h"] < 200):
            continue
        if src not in out:
            out.append(src)
    # real photos are usually JPEG/WebP; PNGs are more often graphics, so they go last
    out.sort(key=lambda u: 1 if re.search(r"\.png(\?|$)", u.lower()) else 0)
    return out[:10]


def _logo(p: _Page) -> str | None:
    for i in p.imgs:
        if "logo" in i["hint"] or "logo" in i["alt"].lower():
            return i["src"]
    for k in ("og:logo",):
        if p.meta.get(k):
            return urljoin(p.base, p.meta[k])
    big = [x for x in p.icons if any(s in x for s in ("180", "192", "512", "apple"))]
    return big[0] if big else None


def _brand_color(p: _Page) -> str | None:
    tc = p.meta.get("theme-color", "")
    if re.fullmatch(r"#[0-9a-fA-F]{6}", tc) and not _greyish(tc):
        return tc.lower()
    for c, _ in p.colors.most_common(20):
        if not _greyish(c):
            return c
    return None


def _greyish(hexc: str) -> bool:
    r, g, b = (int(hexc[i:i + 2], 16) for i in (1, 3, 5))
    return max(r, g, b) - min(r, g, b) < 28 or max(r, g, b) < 40 or min(r, g, b) > 225


def _services(pages: list[_Page]) -> list[str]:
    seen, out = set(), []
    for p in pages:
        for t in p.h2 + p.items:
            k = t.lower().strip(" :-|•")
            if (k in NAV_WORDS or k in seen or len(k) < 3 or len(k) > 48 or k.count(" ") > 6
                    or re.search(r"\d{3}|@|©|copyright|http|\.com|cookie|privacy|all rights", k)):
                continue
            seen.add(k)
            out.append(t.strip(" :-|•"))
    return out[:10]


def _sub_links(home: _Page, root: str) -> list[str]:
    want = ("contact", "about", "services", "our-services", "what-we-do")
    out = []
    host = urlparse(root).hostname
    for href, text in home.links:
        full = urljoin(root, href)
        u = urlparse(full)
        if u.hostname != host or u.scheme not in ("http", "https"):
            continue
        key = (u.path + " " + text).lower()
        if any(w in key for w in want) and full.split("#")[0] not in out and full.split("#")[0].rstrip("/") != root.rstrip("/"):
            out.append(full.split("#")[0])
    out.sort(key=lambda x: 0 if "contact" in x.lower() else 1)
    return out[:3]


# ------------------------------------------------------------------ scoring
def _checks(home: _Page, final: str, secs: float, size: int, phones: list[str], html: str, city: str = "") -> list[dict]:
    visible = " ".join(home.text).lower()
    where = (city or "").rsplit(" ", 1)[0].strip().lower() if city and len(city.rsplit(" ", 1)[-1]) == 2 else (city or "").lower()
    seo_text = " ".join([home.title, home.meta.get("description", "")] + home.h1).lower()
    year = db.now().year
    years = [int(y) for y in re.findall(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?(\d{4})", html, flags=re.I)
             if 1995 < int(y) <= year]
    gen = home.meta.get("generator", "").lower()
    c = [
        ("https", final.startswith("https://"), "Secure connection (https)",
         "Your site isn't secure (no https). Browsers mark it 'Not secure', which scares visitors off.",
         "Your site loads securely over https."),
        ("mobile", bool(home.meta.get("viewport")), "Works on phones",
         "Your site isn't set up for phones, so most visitors have to pinch and zoom. Most local searches happen on a phone.",
         "Your site is set up for phones."),
        ("speed", secs < 2.5 and size < 1_500_000, "Loads fast",
         f"Your homepage took {secs:.1f} seconds and {size / 1e6:.1f} MB to load. Google's research: going from 1 to 3 seconds raises the chance a visitor leaves by 32%.",
         f"Your homepage loaded in {secs:.1f} seconds."),
        ("call", bool(phones), "Tap-to-call button",
         "There's no tap-to-call link, so phone visitors have to copy your number by hand.",
         "Visitors can tap to call you."),
        ("headline", bool(home.h1) and len(home.h1[0]) <= 90, "Clear headline",
         "There's no clear headline saying what you do and where. Visitors decide in about 10 seconds whether to stay.",
         "Your page has a clear main headline."),
        ("modern", not (home.legacy["font"] or home.legacy["center"] or home.legacy["flash"] or home.legacy["frameset"]
                        or home.legacy["table"] > 3), "Modern build",
         "Your site uses outdated building blocks (old table/font layouts), which look dated and break on phones.",
         "Your site is built with modern code."),
        ("fresh", not years or max(years) >= year - 1, "Looks up to date",
         f"Your footer still says {max(years) if years else ''}, so the site looks unattended.", "Your site looks current."),
        ("description", bool(home.meta.get("description")), "Google description",
         "No description is set for Google, so search results show random text from your page.",
         "You have a description for Google search results."),
        ("cta", home.buttons >= 1 or any(re.search(r"\b(call|book|quote|schedule|order|appointment)\b", t.lower())
                                          for _, t in home.links), "Clear next step",
         "There's no obvious button to call, book or get a quote, so visitors have to work out what to do next.",
         "There's a clear button to take the next step."),
        ("reviews", bool(re.search(r"review|testimonial|what (our )?(customers|clients) say|★|5[- ]star", visible)),
         "Reviews on your site",
         "Your homepage doesn't show a single review. 97% of people read reviews before choosing a local business (BrightLocal, 2026).",
         "Your homepage shows reviews."),
        ("local", not where or where in seo_text, "Says where you are",
         f"Your page title and headline don't mention {where.title()}, so Google and visitors can't tell at a glance that you serve the area.",
         "Your page says where you are."),
        ("schema", bool(re.search(r'"@type"\s*:\s*"[A-Za-z]*(LocalBusiness|Store|Restaurant|Salon|Shop|Dentist|Contractor|Service|AutoRepair|Plumber|Electrician)"', html)),
         "Business details for Google",
         "Your site doesn't give Google structured business details (name, hours, location), which helps you show up in local results.",
         "Your site gives Google structured business details."),
        ("share", bool(home.meta.get("og:image")), "Share preview",
         "When someone shares your link by text or on Facebook, no preview picture shows up.",
         "Your link shows a preview picture when shared."),
    ]
    if "wix" in gen or "squarespace" in gen or "weebly" in gen or "godaddy" in gen:
        pass  # a builder is fine; the checks above judge the result, not the tool
    return [{"key": k, "passed": bool(ok), "label": lab, "finding": good if ok else bad} for k, ok, lab, bad, good in c]


WEIGHTS = {"https": 12, "mobile": 20, "speed": 14, "call": 12, "headline": 10, "modern": 12, "fresh": 6,
           "description": 6, "cta": 8, "reviews": 10, "local": 6, "schema": 4, "share": 3}


def score(checks: list[dict]) -> int:
    total = sum(WEIGHTS[c["key"]] for c in checks)
    return round(sum(WEIGHTS[c["key"]] for c in checks if c["passed"]) / total * 100) if total else 0


# ------------------------------------------------------------------ main entry
def fake_site(url: str) -> dict:
    """Offline stand-in (GOOGLE_PLACES_API_KEY=fake): a dated small-business site with a published email."""
    checks = [{"key": k, "passed": k in ("https", "description"), "label": k, "finding": f"{k} finding"} for k in WEIGHTS]
    return {"url": url, "final_url": url, "domain": urlparse(url).hostname or "example.com", "title": "Welcome",
            "description": "Family-owned since 1998.", "headline": "Welcome to our website", "subheads": ["Our services"],
            "paragraphs": ["We are a family-owned shop serving the neighborhood since 1998 with honest work and fair prices "
                           "for every customer who walks through our door."],
            "services": ["Gel", "Pedicure", "Brakes"], "emails": ["owner@example.com"], "phones": ["+18135550100"],
            "socials": {"facebook": "https://www.facebook.com/examplebiz", "instagram": "https://www.instagram.com/examplebiz"},
            "logo": "https://example.com/logo.png", "photos": ["https://example.com/a.jpg", "https://example.com/b.jpg"],
            "og_image": None, "color": "#1d6f42", "checks": checks, "score": score(checks), "load_s": 4.2,
            "bytes": 2_400_000, "pages_read": 2, "checked_at": db.iso()}


def analyze(url: str, city: str = "") -> dict:
    """Read the site and return everything the redesign + pitch need. Raises FetchError when unreadable."""
    if config.GOOGLE_PLACES_API_KEY.lower() == "fake" and "example.com" in url:
        return fake_site(url)
    if not url.startswith("http"):
        url = "https://" + url
    if not allowed(url):
        raise FetchError("robots.txt asks us not to read this site")
    final, html, secs, size = fetch(url)
    if _root(urlparse(final).hostname) != _root(urlparse(url).hostname):
        raise FetchError("the listed website redirects to a different domain")
    home = _parse(final, html)
    visible = _clean(" ".join(home.text))
    if PARKED.search(visible[:3000] + " " + home.title):
        raise FetchError("parked or for-sale domain")
    if len(visible) < 400:  # built entirely by scripts, or a blank page: we can't judge it fairly, so we don't pitch it
        raise FetchError("too little readable text to judge fairly")
    pages = [home]
    for sub in _sub_links(home, final):
        try:
            if allowed(sub):
                f, h, _, _ = fetch(sub, timeout=8)
                pages.append(_parse(f, h))
        except Exception:  # noqa: BLE001
            continue
    phones = _phones(pages)
    checks = _checks(home, final, secs, size, phones, html, city)
    logo = _logo(home)
    domain = urlparse(final).hostname or ""
    paras = []
    for p in pages:
        for t in p.paras:
            if t not in paras and not BOILERPLATE.search(t):
                paras.append(t)
    return {
        "url": url, "final_url": final, "domain": domain, "title": home.title[:120],
        "description": _clean(home.meta.get("description") or home.meta.get("og:description", ""))[:300],
        "headline": home.h1[0][:120] if home.h1 else "", "subheads": home.h2[:8], "paragraphs": paras[:8],
        "services": _services(pages), "emails": _emails(pages, domain), "phones": phones, "socials": _socials(pages),
        "logo": logo, "photos": _photos(home, logo) + [x for p in pages[1:] for x in _photos(p, logo)][:4],
        "og_image": urljoin(final, home.meta["og:image"]) if home.meta.get("og:image") else None,
        "color": _brand_color(home), "checks": checks, "score": score(checks), "load_s": round(secs, 2),
        "bytes": size, "pages_read": len(pages), "checked_at": db.iso(),
    }


def safe_analyze(url: str, city: str = "") -> dict | None:
    try:
        return analyze(url, city)
    except Exception as e:  # noqa: BLE001
        log.info("site check skipped for %s: %s", url, e)
        return None
