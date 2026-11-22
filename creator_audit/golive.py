"""Go-live check: python -m creator_audit check

Tests every connection the live system needs and prints one line per item: OK / FIX / WAIT / INFO.
It never sends an email, never charges anything and makes no Google Places call (those count against the free cap).
Exit code 1 if anything required says FIX.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import config, db

PLACEHOLDER_ADDR = "123 Example St"


def _line(status: str, item: str, detail: str = "") -> tuple[str, str, str]:
    print(f"  {status:<5} {item:<28} {detail}")
    return status, item, detail


def run() -> int:
    out = []
    print("Reelkix audit system: go-live check\n")

    # ---- database
    if not config.DATABASE_URL:
        out.append(_line("FIX", "DATABASE_URL", "not set (Neon connection string)"))
    else:
        try:
            db.init()
            n = db.q1("SELECT COUNT(*) n FROM orders")["n"]
            out.append(_line("OK", "database", f"connected, tables ready ({n} orders)"))
        except Exception as e:  # noqa: BLE001
            out.append(_line("FIX", "database", f"can't connect: {str(e)[:120]}"))

    # ---- site + admin + legal
    out.append(_line("OK" if config.BASE_URL.startswith("https://") else "FIX", "BASE_URL", config.BASE_URL))
    out.append(_line("OK" if config.WAKE_LINKS else "INFO", "WAKE_LINKS", "on" if config.WAKE_LINKS else "off (fine only if the server never sleeps)"))
    out.append(_line("FIX" if config.ADMIN_PASSWORD in ("", "change-me") else "OK", "ADMIN_PASSWORD",
                     "still the default" if config.ADMIN_PASSWORD in ("", "change-me") else "set"))
    out.append(_line("FIX" if PLACEHOLDER_ADDR in config.BUSINESS_ADDRESS else "OK", "BUSINESS_ADDRESS",
                     "still the example address (required in outreach emails)" if PLACEHOLDER_ADDR in config.BUSINESS_ADDRESS else "set"))
    out.append(_line("OK" if config.ADMIN_EMAIL else "INFO", "ADMIN_EMAIL", config.ADMIN_EMAIL or "not set: no weekly summary or alerts"))

    # ---- stripe
    if not config.STRIPE_SECRET_KEY:
        out.append(_line("FIX", "STRIPE_SECRET_KEY", "not set: checkout is closed"))
    else:
        try:
            import stripe
            stripe.api_key = config.STRIPE_SECRET_KEY
            acct = stripe.Account.retrieve()
            live = config.STRIPE_SECRET_KEY.startswith(("sk_live_", "rk_live_"))
            out.append(_line("OK" if live else "INFO", "Stripe key",
                             f"{'LIVE' if live else 'TEST mode'} account {getattr(acct, 'id', '?')}"))
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            # a restricted key may not read the account but can still create Checkout Sessions
            out.append(_line("INFO" if "permission" in msg.lower() else "FIX", "Stripe key", msg[:120]))
    out.append(_line("OK" if config.STRIPE_WEBHOOK_SECRET.startswith("whsec_") else "FIX", "STRIPE_WEBHOOK_SECRET",
                     "set" if config.STRIPE_WEBHOOK_SECRET.startswith("whsec_") else "missing (Stripe -> Webhooks -> signing secret)"))

    # ---- email
    if not config.SMTP_HOST:
        out.append(_line("FIX", "email (SMTP)", "SMTP_HOST not set: nothing can be emailed"))
    elif not config.SMTP_PASSWORD:
        out.append(_line("FIX", "email (SMTP)", f"{config.SMTP_HOST}: SMTP_PASSWORD missing (the Resend API key)"))
    else:
        try:
            import smtplib
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as s:
                if config.SMTP_TLS:
                    s.starttls()
                s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            out.append(_line("OK", "email (SMTP)", f"logged in to {config.SMTP_HOST}:{config.SMTP_PORT} (nothing sent)"))
        except Exception as e:  # noqa: BLE001
            out.append(_line("FIX", "email (SMTP)", f"{config.SMTP_HOST}:{config.SMTP_PORT}: {str(e)[:110]}"))
    out.append(_line("INFO", "send-from", f"{config.MAIL_FROM} / outreach {config.OUTREACH_FROM}"))

    # ---- google
    yk = config.YOUTUBE_API_KEY
    if not yk or yk == "fake":
        out.append(_line("FIX", "YOUTUBE_API_KEY", "not set (or 'fake')"))
    else:
        try:
            import requests
            r = requests.get("https://www.googleapis.com/youtube/v3/videoCategories",
                             params={"part": "snippet", "regionCode": "US", "key": yk}, timeout=20)
            out.append(_line("OK" if r.status_code == 200 else "FIX", "YouTube Data API",
                             "key works (1 quota unit)" if r.status_code == 200 else f"HTTP {r.status_code}: {r.text[:100]}"))
        except Exception as e:  # noqa: BLE001
            out.append(_line("FIX", "YouTube Data API", str(e)[:120]))
    pk = config.GOOGLE_PLACES_API_KEY
    out.append(_line("FIX" if not pk or pk == "fake" else "OK", "GOOGLE_PLACES_API_KEY",
                     "not set (or 'fake'): website previews and local scan are off" if not pk or pk == "fake"
                     else f"set (not called here; monthly caps {config.PLACES_MONTHLY_CAP_SEARCH}/{config.PLACES_MONTHLY_CAP_DETAILS})"))
    if config.NETLIFY_AUTH_TOKEN and config.NETLIFY_SITES_ID:
        try:
            import requests
            r = requests.get(f"https://api.netlify.com/api/v1/sites/{config.NETLIFY_SITES_ID}",
                             headers={"Authorization": f"Bearer {config.NETLIFY_AUTH_TOKEN}"}, timeout=20)
            out.append(_line("OK" if r.ok else "FIX", "Netlify (paid sites host)",
                             f"site {r.json().get('name')} reachable" if r.ok else f"HTTP {r.status_code}: check the token and site id"))
        except Exception as e:  # noqa: BLE001
            out.append(_line("FIX", "Netlify (paid sites host)", str(e)[:120]))
    else:
        out.append(_line("FIX", "Netlify (paid sites host)",
                         "NETLIFY_AUTH_TOKEN / NETLIFY_SITES_ID not set: paid sites are served by the sleeping server"))
    out.append(_line("INFO", "ANTHROPIC_API_KEY", "set (paid per report)" if config.ANTHROPIC_API_KEY else "not set: free template writer"))

    # ---- premium video tools (the GitHub Action installs these itself; on Render they're not needed)
    from . import video
    out.append(_line("OK" if video.host_clips_ready() else "INFO", "video host clips",
                     f"'{config.VIDEO_MASCOT}' clips present" if video.host_clips_ready()
                     else f"no '{config.VIDEO_MASCOT}' clips: videos render slides + voice"))
    out.append(_line("INFO", "ffmpeg (video)", shutil.which("ffmpeg") or "not on this machine (the GitHub Action installs it)"))
    kd = Path.home() / ".cache" / "kokoro"
    out.append(_line("INFO", "Kokoro voice (video)", "model present" if (kd / "kokoro-v1.0.int8.onnx").exists()
                     else "not on this machine (the GitHub Action downloads it)"))

    fixes = [o for o in out if o[0] == "FIX"]
    print(f"\n{len(fixes)} to fix." if fixes else "\nAll required items OK. Ready to go live.")
    return 1 if fixes else 0
