# Creator Audit System

Python backend for the spec in `Creator Audit System — Build Spec.pdf`: paid YouTube audits that run
from checkout to inbox with no manual step, plus a built-in outreach system (daily scanner → free
teaser audits → contact review → automatic emailing and follow-up → unlock/upgrade sales).

## Run it locally (fully offline)

```bash
pip install -r requirements.txt
```
```bash
cp .env.example .env
```
```bash
python -m creator_audit all
```

Open http://localhost:8000/audit. With the default `.env` everything is simulated: checkout is a dev
checkout, YouTube data is synthetic (`YOUTUBE_API_KEY=fake`), reports use the template writer, and
emails land in `data/outbox/*.eml`. Admin is at `/admin` (user `admin`, password from `ADMIN_PASSWORD`).

Make a test export with `python -m creator_audit sample-csv sample.csv` and upload it with channel
`https://www.youtube.com/@democreator`.

Tests: `python tests/test_e2e.py` (standard + premium orders, CSV rejections, number check, outreach
scan → teaser → review → send → open/click → unlock → paid, unsubscribe/DNC, scheduled tasks,
video worker API, data deletion).

## Going live

| Setting | What it turns on |
|---|---|
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | Real Stripe Checkout. Point a webhook at `/stripe/webhook` for `checkout.session.completed` and `charge.refunded`. |
| `ANTHROPIC_API_KEY` | Claude (`claude-opus-5-5`, structured JSON output, server-side refusal fallback) writes the report. |
| `YOUTUBE_API_KEY` | Real YouTube Data API v3 key. |
| `SMTP_*` | Any SMTP provider (Resend, Postmark, SES, …). Set up SPF/DKIM/DMARC on the sending domain. |
| `BASE_URL` | Public URL of this server — used in every email link. |

Processes: `python -m creator_audit all` runs web + worker + scheduler in one process. For more load,
run `serve`, `worker` and `scheduler` separately (they share the SQLite file in `DATA_DIR`).

**Netlify:** keep reelkix.com on Netlify and proxy the audit section to this server:

```toml
[[redirects]]
  from = "/audit/*"
  to = "https://audit-api.reelkix.com/:splat"
  status = 200
```
(or simply link the "Get an audit" button to `https://audit-api.reelkix.com/audit`).

## How it maps to the spec

- **Audit engine** — `metrics.py` (pandas: cadence, momentum, outliers + traits, packaging, retention,
  format mix, conversion) → `scoring.py` (five 0–100 sub-scores, weights 20 each in config, ranked
  problems with a `fix_type` tag) → `narrative.py` (Claude writes words only) → `numcheck.py` (every
  number in the text must match the metrics; failing sections are regenerated once, a second failure
  flags the job instead of sending).
- **Customer flow** — `/audit` → `/checkout` → `/upload/<one-time token>` (3-step export guide, instant
  CSV validation with fix-it messages, Premium goals form) → `/status/<token>` → worker → report page
  `/r/<token>` + PDF + delivery email with upsell → 30/60-day re-audit emails.
- **Tracking** — `audit_log` (job, channel, tier, metrics JSON, score, report/prompt version, tokens),
  `advice` (fix types), `outcome_checks` at 30/60/90 days, advice grading on `/admin`, funnel per niche,
  sales/refunds, weekly summary email to `ADMIN_EMAIL`.
- **Privacy** — uploaded CSVs deleted right after the report; `/delete-my-data`; privacy + terms pages
  (YouTube API Services / Google Privacy Policy / YouTube ToS); outreach API data purged or scrubbed after
  `API_DATA_RETENTION_DAYS`.
- **Mascot video (Phase 2 hook)** — teaser/premium jobs queue a script in `video_jobs`. A render worker on
  the Windows PC polls `POST /api/video-jobs/claim` and uploads the MP4 to `/api/video-jobs/<id>/done`
  (header `X-Worker-Key`). The report page shows the video once it exists.

## Outreach: what's automatic and what isn't

Automatic: daily scan (search.list within quota, filters: active in last 30 days, subscriber range,
recent median views below 80% of earlier median), teaser audit + private noindex link, sending to
approved contacts within send hours and a daily cap, one follow-up after 4 days unless they clicked,
open/click tracking, one-click unsubscribe → do-not-contact list (checked before every send),
funnel stages through to paid.

Not automatic, on purpose: **finding the email.** The YouTube API gives no emails, its policies forbid
harvesting contact info, and scraping About pages risks the API key. Emails come from a person (review
queue at `/admin/queue`) or a lead-source CSV import, each with a recorded source. Set
`OUTREACH_AUTO_APPROVE_IMPORTS=true` to let imported leads go straight to sending without a review.

Every outreach email carries an honest subject, the business address, an unsubscribe link and
`List-Unsubscribe` headers (CAN-SPAM). Not legal advice — check with a lawyer before scaling.

## Local Business Scanner (Google) + $200 websites

Flow, all automatic once keys and a postcard budget are set:

1. **Daily scan** (`local.py`, at `LOCAL_SCAN_HOUR`): Google Places Text Search for each
   `LOCAL_CATEGORIES` × `LOCAL_CITIES` pair, up to `LOCAL_SEARCHES_PER_DAY` (33/day ≈ the 1,000/month free
   Enterprise tier). Keeps operating businesses with no website (`LOCAL_TARGET=no_website_or_weak` also
   keeps businesses with a website but a weak listing).
2. **Check-up**: website, hours, photos, rating, review count, phone, and optionally whether an AI assistant
   names them (`LOCAL_AI_CHECK=true`, uses Claude with web search; costs per check). Score out of 100,
   each failed check comes with a plain fix. Fixed templates, so no AI-written numbers.
3. **Mock homepage** at `/m/<token>`: category theme (beauty, barber, food, auto, health, trades, default),
   name, sample services, hours, tap-to-call and directions. No Google photos, "Concept by Reelkix, not
   affiliated" banner, noindex, private link, expires after `MOCK_EXPIRY_DAYS`, no booking/order/payment forms.
4. **Contact**:
   - *Postcards* (fully hands-off): Lob prints and mails a 4×6 card with a QR code to the check-up page,
     within `POSTCARD_MONTHLY_BUDGET_CENTS` (0 = off). Without `LOB_API_KEY` the cards are written to
     `data/postcards/` so you can see them. Each card has an opt-out URL.
   - *Email* (optional): paste an email the business publishes (Facebook, Instagram, Yelp) at `/admin/local`,
     or import a CSV (`place_id,email,source`). Approved contacts get one email + one follow-up with the
     CAN-SPAM footer.
   - No automated texts and no calls (spec).
5. **Check-up page** `/b/<token>`: score, findings + fixes, the mock site embedded, and either
   "Get my website · $200" (`LOCAL_GATE=paywall`) or "Book a call" (`LOCAL_GATE=consult`, `CONSULT_URL`).
6. **After payment**: the owner gets a setup form prefilled once from Google, confirms their details, and
   the site goes live at `/s/<slug>` (indexable, no banner). A confirmation email covers adding the link to
   their Google Business Profile and pointing their own domain (CNAME → `SITE_DOMAIN_HINT`).
7. **Tracking**: funnel per category and city (scanned → no website → built → postcard/email → visited →
   consult/paid → opt-out) at `/admin/local` and in the weekly summary.

**Google data rules:** only the place ID is stored; name, address, hours, rating etc. are re-fetched from
Place Details when a page is shown or a card is sent (kept in memory for `DETAILS_CACHE_SECONDS`). After a
sale, the site uses the owner's own answers, not Google's. Unvisited previews expire and are deleted 30
days after that. **Before launch, confirm in Google's Maps terms** that the short in-memory cache and a mock
page built from fetched details are allowed (the spec flags both).

Costs to expect: Place Details is also an Enterprise SKU. It's called for every preview view, postcard and
admin list row, but the cache absorbs repeat calls. Postcards cost about $0.70–1.00 each through Lob.

What still needs a person: answering replies, connecting a custom domain when an owner asks, and edits
they request by email.

## Still open (from the spec)

Product name, Premium price (placeholder $49), keep or drop the $4.99 unlock (`UNLOCK_ENABLED`),
host, where mascot videos render, niches and subscriber range, YouTube data-retention window
(default 30 days — confirm in the Developer Policies), scoring benchmarks (starting values in `scoring.py`).
Local: categories and cities, paywall vs consult, website price ($200 set), mock expiry (30 days),
postcard budget (off until set). Not built: Instagram/TikTok inputs (Phase 3).
