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

Tests: `python tests/test_e2e.py` and `python tests/test_local.py` run on SQLite; `python tests/run_pg.py`
runs both again on a throwaway Postgres 16 (`pip install pgserver "psycopg[binary]"`), the production database type.
`test_e2e.py` covers standard + premium orders, CSV rejections, number check, outreach
scan → teaser → review → send → open/click → unlock → paid, unsubscribe/DNC, scheduled tasks,
video worker API, data deletion).

## Going live

| Setting | What it turns on |
|---|---|
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | Real Stripe Checkout. Point a webhook at `/stripe/webhook` for `checkout.session.completed` and `charge.refunded`. |
| `ANTHROPIC_API_KEY` | Claude (`claude-opus-5-5`, structured JSON output, server-side refusal fallback) writes the report. |
| `YOUTUBE_API_KEY` | Real YouTube Data API v3 key. |
| `SMTP_*` | Any SMTP provider (Resend, Postmark, SES, …). Set up SPF/DKIM/DMARC on the sending domain. |
| `BASE_URL` | Public URL used in every email link: `https://reelkix.com` (Netlify proxies these pages here). |
| `DATABASE_URL` | Postgres (Neon free tier). Blank = SQLite in `DATA_DIR`. |

### Free hosting ($0/month)

| Piece | Free service | Notes |
|---|---|---|
| Web | Render free web service (`render.yaml`, `python -m creator_audit web`) | Sleeps after 15 min idle. **No uptime pinger**: free hours are shared with app.reelkix.com. |
| Database | Neon free Postgres | Uploads and mascot videos are stored in it too (Render's free disk is wiped on restart); PDFs are rebuilt on demand. |
| Schedule | GitHub Action every 3 hours (`.github/workflows/cron.yml`, `python -m creator_audit cron`) | Scans, sending, follow-ups, outcome checks, expiry, weekly summary, queued teaser jobs. |
| Pages | reelkix.com on Netlify | `/audit` and `/website-preview` are static pages that wake the backend on load; other paths are proxied (`_redirects`). Emailed links go through `reelkix.com/go/#<path>`, which waits for the backend to wake (`WAKE_LINKS=true`). |

Free-tier guards: Google Places calls are counted per month and refused past `PLACES_MONTHLY_CAP_SEARCH` /
`PLACES_MONTHLY_CAP_DETAILS` (900 each, under Google's ~1,000 free); `ANTHROPIC_API_KEY` blank = template writer;
no dev checkout and no fake "sent" emails once `DATABASE_URL` is set. Stripe only charges per sale.

**Stripe (shared account):** our Checkout Sessions carry `metadata.app = reelkix-audit`; the webhook and
success page ignore every other session (Payment Links for other Reelkix products). Webhook endpoint:
`https://reelkix-audit.onrender.com/stripe/webhook`, events `checkout.session.completed` and `charge.refunded`.

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

## Premium ($49): plans + video walkthrough

- `planner.py` builds a 30-day "fix" plan (one week per top problem, then double down, day-30 check-in) and a
  60-day "grow" plan (series, goal push, format experiment, habit; day-45 check-in, day-60 re-audit) from the
  creator's own problems, best videos, format mix and goals form. Rule-based and free.
- Title and thumbnail fixes are rule-based; with `ANTHROPIC_API_KEY` set (optionally `CLAUDE_TIERS=premium`) they
  become full AI rewrites.
- **Adding the host (new cast member):** export three 3:4 clips (e.g. 1080x1440, 24 fps, about 3-6 s, studio background)
  named `<name>_talk.mp4`, `<name>_point.mp4`, `<name>_present.mp4` into `creator_audit/video_assets/`, then set
  `VIDEO_MASCOT=<name>` and `VIDEO_HOST_NAME=<her name>` (Render env + the GitHub Action). Until then videos render
  with slides, voice and a Reelkix panel where the host goes.
- `video.py` renders the host's walkthrough (slides + `video_assets/<VIDEO_MASCOT>_*.mp4` + Kokoro voice `VIDEO_VOICE`, default `af_heart`, + ffmpeg,
  ~2 min, ~2 MB, stored in the database). The GitHub Action renders pending videos on its 3-hourly run and emails
  the customer; promise is 2 days (`VIDEO_PROMISE_DAYS`), admin alert after `VIDEO_ALERT_HOURS` (36). Any PC can
  also run `python -m creator_audit videos` (needs ffmpeg + `pip install -r requirements-video.txt`).

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

## Local Business Scanner (Google) + websites ($249 DIY, $749 done-for-you)

Online only (owner decision): no postcards, no texts, no calls.

1. **Daily scan** (`local.py`, at `LOCAL_SCAN_HOUR`, run by the cron): Google Places Text Search for each
   `LOCAL_CATEGORIES` × `LOCAL_CITIES` pair, up to `LOCAL_SEARCHES_PER_DAY`. Keeps operating businesses with no
   website (`LOCAL_TARGET=no_website_or_weak` also keeps weak listings).
2. **Self-serve preview** `/website-preview`: a business types its name + city, picks itself from up to 5
   matches, and lands on its own check-up `/b/<token>`. Rate-limited per visitor (`SELF_SERVE_PER_IP_PER_DAY`)
   and per day (`SELF_SERVE_PER_DAY`); a place that already has a preview reuses it.
3. **Check-up**: website, hours, photos, rating, review count, phone, optionally AI mentions (`LOCAL_AI_CHECK`).
   Score out of 100 with a plain fix per failed check; fixed templates, no AI-written numbers.
4. **Mock homepage** `/m/<token>`: category theme, name, sample services, hours, tap-to-call, directions. No
   Google photos, "Concept by Reelkix, not affiliated" banner, noindex, private link, expires after
   `MOCK_EXPIRY_DAYS`, no booking/order/payment forms.
5. **Email contact**: paste an email the business publishes at `/admin/local` or import a CSV
   (`place_id,email,source`). Approved contacts get one email + one follow-up with the CAN-SPAM footer.
6. **Purchase** "Get my website · $249" (or "Do it for me · $749", which emails you to do the work) → Stripe → setup form (prefilled once from Google) → live at `/s/<slug>`, published as a static page to a free Netlify site (`NETLIFY_AUTH_TOKEN` + `NETLIFY_SITES_ID`; reelkix.com proxies `/s/*` there) → buyer gets a check-up PDF with a 30-day plan at `/bp/<token>.pdf`. Edit a site or set its custom domain at `/admin/site/<id>`.
7. **Tracking** per category and city at `/admin/local` and in the weekly summary.

**Google data rules:** only the place ID is stored; details are re-fetched when a page is shown (in-memory cache
for `DETAILS_CACHE_SECONDS`). After a sale the site uses the owner's own answers. Unvisited previews expire and
are deleted 30 days later. **Before launch, confirm in Google's Maps terms** that the short cache and a mock page
built from fetched details are allowed.

What still needs a person: answering replies, connecting a custom domain when an owner asks, edits by email.

## Still open (from the spec)

Product name, keep or drop the $4.99 unlock (`UNLOCK_ENABLED`),
host, where mascot videos render, niches and subscriber range, YouTube data-retention window
(default 30 days — confirm in the Developer Policies), scoring benchmarks (starting values in `scoring.py`).
Local: categories and cities, paywall vs consult, mock expiry (30 days). Decided: online only, $249 DIY / $749 done-for-you website (2026-11-22),
existing Stripe account, self-serve preview page built. Not built: Instagram/TikTok inputs (Phase 3).
