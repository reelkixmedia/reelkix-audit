"""All settings come from environment variables (or a .env file next to the project).

Anything left blank falls back to a safe local/dev behaviour:
  - no STRIPE_SECRET_KEY      -> "dev checkout" that marks orders paid instantly
  - no ANTHROPIC_API_KEY      -> deterministic template writer (still number-checked)
  - YOUTUBE_API_KEY=fake      -> synthetic YouTube data for local testing
  - no SMTP_HOST              -> emails are written to data/outbox/*.eml
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def _s(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _b(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in _s(name, default).split(",") if x.strip()]


# ---------- paths ----------
DATA_DIR = Path(_s("DATA_DIR", str(ROOT / "data")))
DB_PATH = DATA_DIR / "audit.db"
# Postgres connection string (Neon free tier). Blank = SQLite file in DATA_DIR (local dev + tests).
DATABASE_URL = _s("DATABASE_URL")
UPLOAD_DIR = DATA_DIR / "uploads"
REPORT_DIR = DATA_DIR / "reports"
OUTBOX_DIR = DATA_DIR / "outbox"
VIDEO_DIR = DATA_DIR / "videos"
for _d in (DATA_DIR, UPLOAD_DIR, REPORT_DIR, OUTBOX_DIR, VIDEO_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------- site ----------
BASE_URL = _s("BASE_URL", "http://localhost:8000").rstrip("/")
SITE_URL = _s("SITE_URL", "https://reelkix.com").rstrip("/")  # main Reelkix site (header/footer links)
# Free hosting sleeps when idle. With WAKE_LINKS on, links we email go through reelkix.com/go/#<path>, a static
# Netlify page that waits for this server to wake up and then forwards. Off locally and in tests.
WAKE_LINKS = _b("WAKE_LINKS", False)


def link(path: str) -> str:
    """Public URL for a page on this server, for emails and redirects that leave the site."""
    return f"{BASE_URL}/go/#{path}" if WAKE_LINKS else f"{BASE_URL}{path}"
PRODUCT_NAME = _s("PRODUCT_NAME", "Creator Audit")  # open decision in the spec
BRAND = _s("BRAND", "Reelkix")
BUSINESS_ADDRESS = _s("BUSINESS_ADDRESS", "Reelkix, 123 Example St, Tampa, FL 33602, USA")
SUPPORT_EMAIL = _s("SUPPORT_EMAIL", "support@reelkix.com")
ADMIN_EMAIL = _s("ADMIN_EMAIL", "")  # weekly summary + flagged-job alerts
ADMIN_USER = _s("ADMIN_USER", "admin")
ADMIN_PASSWORD = _s("ADMIN_PASSWORD", "change-me")
VIDEO_WORKER_KEY = _s("VIDEO_WORKER_KEY", "")  # shared secret for the mascot render worker
# Premium video walkthrough (video.py): rendered by the GitHub Action, promised within 2 days
VIDEO_MASCOT = _s("VIDEO_MASCOT", "koala")     # clips in creator_audit/video_assets/<mascot>_{talk,point,present}.mp4
VIDEO_HOST_NAME = _s("VIDEO_HOST_NAME", "")   # what the host calls herself ("Hi, I'm ..."); blank = no name
VIDEO_VOICE = _s("VIDEO_VOICE", "af_heart")   # Kokoro voice (af_heart = the female voice approved for Ms. Ola)
VIDEO_SPEED = _f("VIDEO_SPEED", 1.0)
VIDEO_PROMISE_DAYS = _i("VIDEO_PROMISE_DAYS", 2)
VIDEO_ALERT_HOURS = _i("VIDEO_ALERT_HOURS", 36)
VIDEO_TEASERS = _b("VIDEO_TEASERS", False)  # mascot clips for free teasers too (off: Premium only)

# ---------- pricing (cents) ----------
PRICE_STANDARD = _i("PRICE_STANDARD_CENTS", 1999)
PRICE_PREMIUM = _i("PRICE_PREMIUM_CENTS", 4900)  # $49, decided 2026-11-16
PREMIUM_ENABLED = _b("PREMIUM_ENABLED", True)  # false = not sold or upsold
PRICE_UNLOCK = _i("PRICE_UNLOCK_CENTS", 499)
UNLOCK_ENABLED = _b("UNLOCK_ENABLED", True)

# ---------- stripe ----------
STRIPE_SECRET_KEY = _s("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = _s("STRIPE_WEBHOOK_SECRET")
# Dev checkout (orders marked paid with no payment) is only allowed locally, never with a production database.
DEV_CHECKOUT = not STRIPE_SECRET_KEY and _b("ALLOW_DEV_CHECKOUT", not DATABASE_URL)
STRIPE_APP_TAG = "reelkix-audit"  # metadata.app on our Checkout Sessions; the shared Stripe account sells other things too

# ---------- claude ----------
ANTHROPIC_API_KEY = _s("ANTHROPIC_API_KEY")
CLAUDE_MODEL = _s("CLAUDE_MODEL", "claude-opus-5-5")
CLAUDE_EFFORT = _s("CLAUDE_EFFORT", "medium")
# Which tiers use Claude when ANTHROPIC_API_KEY is set (e.g. "premium" = only $49 orders, so cost only on those sales)
CLAUDE_TIERS = _list("CLAUDE_TIERS", "teaser,unlock,standard,premium")
PROMPT_VERSION = "p1.0"
REPORT_VERSION = "r1.0"

# ---------- youtube ----------
YOUTUBE_API_KEY = _s("YOUTUBE_API_KEY")
SHORTS_MAX_SECONDS = _i("SHORTS_MAX_SECONDS", 180)
MAX_VIDEOS_PULLED = _i("MAX_VIDEOS_PULLED", 200)

# ---------- scoring ----------
WEIGHTS = {
    "consistency": _f("W_CONSISTENCY", 20),
    "momentum": _f("W_MOMENTUM", 20),
    "packaging": _f("W_PACKAGING", 20),
    "retention": _f("W_RETENTION", 20),
    "conversion": _f("W_CONVERSION", 20),
}
TARGET_UPLOADS_PER_WEEK = _f("TARGET_UPLOADS_PER_WEEK", 2.0)

# ---------- email ----------
SMTP_HOST = _s("SMTP_HOST")
SMTP_PORT = _i("SMTP_PORT", 587)
SMTP_USER = _s("SMTP_USER")
SMTP_PASSWORD = _s("SMTP_PASSWORD")
SMTP_TLS = _b("SMTP_TLS", True)
# No SMTP: emails are written to data/outbox (local only). With a production database they are refused instead,
# so nobody is marked "emailed" when nothing went out.
OUTBOX_ALLOWED = _b("ALLOW_OUTBOX", not DATABASE_URL)
MAIL_FROM = _s("MAIL_FROM", f"{BRAND} Audits <audits@reelkix.com>")
OUTREACH_FROM = _s("OUTREACH_FROM", MAIL_FROM)

# ---------- outreach ----------
OUTREACH_NICHES = _list("OUTREACH_NICHES", "fitness,cooking,gaming")
OUTREACH_MIN_SUBS = _i("OUTREACH_MIN_SUBS", 1_000)
OUTREACH_MAX_SUBS = _i("OUTREACH_MAX_SUBS", 100_000)
OUTREACH_ACTIVE_DAYS = _i("OUTREACH_ACTIVE_DAYS", 30)
OUTREACH_DECLINE_RATIO = _f("OUTREACH_DECLINE_RATIO", 0.8)  # recent median < 80% of earlier median
OUTREACH_SEARCHES_PER_DAY = _i("OUTREACH_SEARCHES_PER_DAY", 60)  # quota: 100 search calls/day
OUTREACH_RESULTS_PER_SEARCH = _i("OUTREACH_RESULTS_PER_SEARCH", 25)
# Spec: contacts are reviewed by hand before sending. Set true to let contacts that
# arrive from a trusted lead-source import skip the review queue.
OUTREACH_AUTO_APPROVE_IMPORTS = _b("OUTREACH_AUTO_APPROVE_IMPORTS", False)
OUTREACH_DAILY_SEND_LIMIT = _i("OUTREACH_DAILY_SEND_LIMIT", 40)
# Local-business pitches: random pause between sends and a cap per pass (set in cron.yml; 0 = off, so admin clicks don't hang)
PITCH_PAUSE_S = (_i("PITCH_PAUSE_MIN_S", 0), _i("PITCH_PAUSE_MAX_S", 0))
PITCH_MAX_PER_PASS = _i("PITCH_MAX_PER_PASS", 0)
OUTREACH_FOLLOWUP_DAYS = _i("OUTREACH_FOLLOWUP_DAYS", 4)
OUTREACH_MAX_FOLLOWUPS = _i("OUTREACH_MAX_FOLLOWUPS", 1)
OUTREACH_SEND_HOURS = (_i("OUTREACH_SEND_START_HOUR", 9), _i("OUTREACH_SEND_END_HOUR", 17))
TEASER_EXPIRY_DAYS = _i("TEASER_EXPIRY_DAYS", 30)

# ---------- local business scanner ----------
GOOGLE_PLACES_API_KEY = _s("GOOGLE_PLACES_API_KEY")  # "fake" = synthetic offline businesses
LOCAL_CATEGORIES = _list("LOCAL_CATEGORIES", "nail salon,barber shop,hair salon,auto repair,restaurant,bakery,plumber,electrician,roofing contractor,painter,locksmith,landscaping,cleaning service,hvac contractor,car wash,food truck")
# "US" = every larger city in all 50 states + DC (creator_audit/us_cities.py); or a comma list like "Tampa FL,Miami FL"
_cities = _s("LOCAL_CITIES", "US")
if _cities.strip().upper() == "US":
    from .us_cities import US as LOCAL_CITIES
else:
    LOCAL_CITIES = [x.strip() for x in _cities.split(",") if x.strip()]
LOCAL_SEARCHES_PER_DAY = _i("LOCAL_SEARCHES_PER_DAY", 15)  # scan budget; self-serve lookups share the monthly cap below
# Hard monthly caps per Places SKU (Google's free allowance is ~1,000/month each for Text Search and Place
# Details Enterprise). Calls past the cap are refused, so the bill stays $0.
PLACES_MONTHLY_CAP_SEARCH = _i("PLACES_MONTHLY_CAP_SEARCH", 900)
PLACES_MONTHLY_CAP_DETAILS = _i("PLACES_MONTHLY_CAP_DETAILS", 900)
LOCAL_TARGET = _s("LOCAL_TARGET", "no_website")  # no_website | no_website_or_weak
# Redesigns: businesses that HAVE a website get it read (sitecheck.py); weak ones get a redesigned preview,
# and the email they publish on their own site is used automatically (Nikko, 2026-11-23: "fully automatic").
REDESIGN_ENABLED = _b("REDESIGN_ENABLED", True)
REDESIGN_MAX_SCORE = _i("REDESIGN_MAX_SCORE", 85)  # below this = at least two real, fixable problems to point at
SITECHECKS_PER_SCAN = _i("SITECHECKS_PER_SCAN", 60)  # their own sites, read politely; free
SITECHECK_BUDGET_S = _i("SITECHECK_BUDGET_S", 420)  # stop reading sites after 7 minutes per scan (Actions minutes)
REDESIGN_AUTO_SEND = _b("REDESIGN_AUTO_SEND", True)  # email found on their own site is approved for sending
LOCAL_SCAN_HOUR = _i("LOCAL_SCAN_HOUR", 7)
LOCAL_GATE = _s("LOCAL_GATE", "paywall")  # paywall ($ site) | consult (book-a-call button)
CONSULT_URL = _s("CONSULT_URL", "")
PRICE_WEBSITE = _i("PRICE_WEBSITE_CENTS", 24900)  # $249 DIY site (owner fills the setup form), decided 2026-11-22
# Done-for-you: we write the copy, pick photos, connect their domain and tidy their Google profile (about 3x DIY)
PRICE_WEBSITE_PRO = _i("PRICE_WEBSITE_PRO_CENTS", 74900)
WEBSITE_PRO_ENABLED = _b("WEBSITE_PRO_ENABLED", True)
WEBSITE_PRO_DAYS = _i("WEBSITE_PRO_DAYS", 3)  # business days promised for done-for-you delivery
MOCK_EXPIRY_DAYS = _i("MOCK_EXPIRY_DAYS", 30)
DETAILS_CACHE_SECONDS = _i("DETAILS_CACHE_SECONDS", 3600)  # in-memory only; confirm vs Maps terms
LOCAL_AI_CHECK = _b("LOCAL_AI_CHECK", False)  # ask Claude (web search) whether AI names them; costs per check
# Paid sites are published as static pages to a separate free Netlify site (always fast, no sleeping server);
# reelkix.com proxies /s/* there. Without a token they are served by this app instead.
NETLIFY_AUTH_TOKEN = _s("NETLIFY_AUTH_TOKEN")
NETLIFY_SITES_ID = _s("NETLIFY_SITES_ID")
SITES_HOST = _s("SITES_HOST", "reelkix-sites.netlify.app")
SITE_DOMAIN_HINT = _s("SITE_DOMAIN_HINT", "reelkix-sites.netlify.app")  # CNAME target shown to buyers
# Self-serve "free website preview": a business looks itself up (one Places call per lookup)
SELF_SERVE_ENABLED = _b("SELF_SERVE_ENABLED", True)
SELF_SERVE_PER_IP_PER_DAY = _i("SELF_SERVE_PER_IP_PER_DAY", 5)
SELF_SERVE_PER_DAY = _i("SELF_SERVE_PER_DAY", 15)  # all visitors together; keeps Places inside the free tier

# ---------- retention / schedule ----------
API_DATA_RETENTION_DAYS = _i("API_DATA_RETENTION_DAYS", 30)  # confirm vs YouTube Developer Policies
SCANNER_HOUR = _i("SCANNER_HOUR", 6)
WEEKLY_SUMMARY_WEEKDAY = _i("WEEKLY_SUMMARY_WEEKDAY", 0)  # Monday
