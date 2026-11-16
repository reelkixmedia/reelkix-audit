# Go live for $0/month: audits + $200 websites inside reelkix.com

Same free pattern as app.reelkix.com. Everything below is free. The only card you may be asked for is Google's (step 5),
and the code caps Google usage below its free allowance.

| Piece | Free service | Status |
|---|---|---|
| Code | GitHub `reelkixmedia/reelkix-audit` (private) | **Done** (pushed) |
| Database | Neon free Postgres | You: step 1 |
| Web app | Render free web service `reelkix-audit` | You: step 2 |
| Payments | Existing Stripe account | You: step 3 |
| Email | Resend (same account and domain as app.reelkix.com) | You: step 4 |
| Google + YouTube keys | Google Cloud | You: steps 5–6 |
| Scheduled work | GitHub Action every 3 hours | You: step 7 |
| Pages on reelkix.com | Netlify (`Downloads\reelkix_site_v51`) | **Claude** deploys after step 7 |

Paste keys only into Render, GitHub or Stripe, never into chat.

**Don't add an UptimeRobot monitor for this service.** Render's 750 free hours are shared with app.reelkix.com.
This service sleeps when idle, and reelkix.com wakes it up for visitors.

---

## 1. Database: Neon (3 min)
1. neon.com → log in (same account as the Recovery Engine) → **New Project**.
2. Name `reelkix-audit`, region **AWS US East 1 (N. Virginia)** → **Create**.
3. On the project dashboard click **Connect** → keep **Connection pooling** ON → **Copy** the string
   (starts with `postgresql://`, contains `-pooler`, ends with `sslmode=require`). This is your `DATABASE_URL`.

## 2. Render: the web app (5 min)
1. dashboard.render.com → **New +** → **Blueprint**.
2. Pick **reelkixmedia/reelkix-audit**. If it isn't listed: **Configure account** → GitHub → give Render access to
   `reelkix-audit` → back to Render.
3. Blueprint name `reelkix-audit` → Render reads `render.yaml`. Fill the boxes it asks for:
   - `DATABASE_URL`: the Neon string from step 1
   - `BUSINESS_ADDRESS`: your mailing address or PO box (shown in every outreach email, legally required)
   - `ADMIN_EMAIL`: `reelkixmedia@gmail.com` (weekly summary + alerts)
   - `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`: leave blank for now (step 3)
   - `SMTP_PASSWORD`: leave blank for now (step 4)
   - `YOUTUBE_API_KEY`, `GOOGLE_PLACES_API_KEY`: leave blank for now (steps 5–6)
4. **Apply**. When it says **Live**, open `https://reelkix-audit.onrender.com/healthz`. It should show `{"ok":true}`.
   **If Render gave the service a different address**, tell Claude: the reelkix.com proxy rules point at this one.
5. Admin password: Render → **reelkix-audit** → **Environment** → `ADMIN_PASSWORD` (made for you) → eye icon.
   Admin pages: `https://reelkix-audit.onrender.com/admin` and `/admin/local` (user `admin`).

## 3. Stripe (existing account, 3 min)
1. dashboard.stripe.com → make sure **Test mode is OFF** (top right).
2. **Developers → Webhooks → Add endpoint**
   - Endpoint URL: `https://reelkix-audit.onrender.com/stripe/webhook`
   - **Select events** → search and tick `checkout.session.completed` and `charge.refunded` → **Add events** → **Add endpoint**.
3. On the new endpoint → **Signing secret → Reveal** → copy → Render → reelkix-audit → **Environment** →
   `STRIPE_WEBHOOK_SECRET` → paste.
4. **Developers → API keys** → **Secret key → Reveal** (or create a restricted key with *Checkout Sessions: Write*)
   → copy → Render `STRIPE_SECRET_KEY` → paste → **Save, rebuild and deploy**.

The app.reelkix.com webhook ignores these sales (it only acts on its own plan prices), and this app ignores every
sale that isn't its own (`metadata.app = reelkix-audit`).

## 4. Email: Resend (2 min)
Uses your verified domain `notify.reelkix.com`. Host, port 2587 and user are already set in `render.yaml`.
1. resend.com → **API Keys → Create API Key** → name `reelkix-audit`, permission **Sending access**, domain
   `notify.reelkix.com` → **Add** → copy.
2. Render → reelkix-audit → **Environment** → `SMTP_PASSWORD` → paste → **Save, rebuild and deploy**.

Resend's free plan allows 100 emails a day, shared with app.reelkix.com. Outreach is capped at 30 a day.

## 5. Google Places key (for the $200 website side, 5 min)
1. console.cloud.google.com → top bar project picker → **New project** `reelkix-audit` → **Create**.
2. **APIs & Services → Library** → search **Places API (New)** → **Enable**. Google asks for a billing account (card).
   Usage stays inside the monthly free allowance because the code stops at 900 calls a month per type.
3. Extra safety net: **APIs & Services → Places API (New) → Quotas** → set **SearchText requests per day** to `40`
   and **GetPlace requests per day** to `40`.
4. **APIs & Services → Credentials → Create credentials → API key** → **Edit API key** → *API restrictions*:
   **Restrict key** → tick **Places API (New)** → **Save** → copy the key.
5. Render → `GOOGLE_PLACES_API_KEY` → paste → **Save, rebuild and deploy**.

## 6. YouTube key (for audits + outreach scan, 2 min, no billing needed)
1. Same Google project → **Library** → **YouTube Data API v3** → **Enable**.
2. **Credentials → Create credentials → API key** → restrict to **YouTube Data API v3** → copy.
3. Render → `YOUTUBE_API_KEY` → paste → **Save, rebuild and deploy**.

## 7. GitHub secrets for the 3-hourly job (3 min)
github.com/reelkixmedia/reelkix-audit → **Settings → Secrets and variables → Actions → New repository secret**,
one at a time, same values as in Render:
`DATABASE_URL`, `BUSINESS_ADDRESS`, `ADMIN_EMAIL`, `YOUTUBE_API_KEY`, `GOOGLE_PLACES_API_KEY`, `SMTP_PASSWORD`.

Then **Actions → audit-cron → Run workflow**. A green check means it works.
(Private repos get 2,000 free Action minutes a month, shared with the Recovery Engine worker. This job uses about 400.)

## 8. Tell Claude "audit backend is live"
Claude then deploys `Downloads\reelkix_site_v51` to Netlify. That adds `/audit`, `/website-preview`, `/go/` and the
proxy rules, plus footer links on every page. Then Claude checks the live pages, the checkout redirect to Stripe, the
webhook, and the self-serve lookup.

---

## Still your call
- **Premium audit:** $49, on (decided 2026-11-16). `PREMIUM_ENABLED=false` in Render hides it again.
- **$4.99 unlock** for outreach teasers: on (`UNLOCK_ENABLED`).
- **Claude-written reports.** Off: free template writer, every number still checked. Adding `ANTHROPIC_API_KEY`
  costs money per report.
- **YouTube niches/subscriber range and local categories/cities:** set in Render env (`OUTREACH_NICHES`, `LOCAL_CATEGORIES`, `LOCAL_CITIES`).
- **Before real outreach:** confirm Google Maps terms (mock pages + short cache) and the YouTube data-retention
  window, and have the audit privacy/terms pages reviewed.
