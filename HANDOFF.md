# Handoff: connect the Audit System to reelkix.com

Paste this whole file into a new Claude chat that is opened on the **Reelkix website folder**. It
explains what has been built, where it is, and what that chat needs to do.

---

## 1. What exists

A Python backend (FastAPI + SQLite) at:

```
C:\Users\a1nik\Downloads\Audit System
```

Read `README.md` in that folder first. It has the full setup and how each part maps to the build spec
(`Creator Audit System — Build Spec.pdf`, same folder).

**Give the new chat access to that folder too**: add it as an extra directory in the chat, or copy
`creator_audit/`, `tests/`, `requirements.txt`, `.env.example` and `README.md` into the site repo.

It contains two products:

**A. YouTube creator audits**
- Paid tiers: Standard $19.99, Premium (placeholder $49), optional $4.99 unlock.
- Flow: `/audit` page → Stripe Checkout → one-time upload link → creator uploads their YouTube Studio CSV →
  worker computes metrics with pandas → Claude writes the explanation → a number check rejects any invented
  number → report page `/r/<token>` + PDF + delivery email with an upsell → 30/60-day follow-ups.
- Outreach: a daily YouTube scan finds channels with falling views and builds a free teaser `/t/<token>`.
  A person adds a published contact email in `/admin/queue`, then sending, the follow-up, tracking and
  unsubscribes run automatically.

**B. Local businesses (Google) + $200 websites**
- A daily Google Places scan finds businesses with no website, builds a listing check-up `/b/<token>`
  (score + fixes) and a mock homepage `/m/<token>`.
- "Get my website · $200" → Stripe → the owner confirms their details on a setup form → the site goes live
  at `/s/<slug>`.
- Contact today: an email the business publishes (entered in `/admin/local` or imported from CSV).
  Postcard code exists but is OFF and **the owner does not want postcards** (see section 4).

Admin: `/admin` (dashboard), `/admin/queue` (YouTube outreach), `/admin/local` (local businesses). Basic
auth with `ADMIN_USER` / `ADMIN_PASSWORD`.

Run locally: `pip install -r requirements.txt`, copy `.env.example` to `.env`, then
`python -m creator_audit all` → http://localhost:8000. Tests: `python tests/test_e2e.py` and
`python tests/test_local.py` (both fully offline).

## 2. What the owner wants

- The audits and the website offer live **inside reelkix.com** (hosted on Netlify), not as a separate-looking site.
- Use the **Stripe account already connected to reelkix.com**.
- **Online only**: no postcards, no texts, no calls.
- As little manual work as possible.

## 3. Tasks for the new chat

1. **Look at the Reelkix site folder**: framework, how it's built and deployed on Netlify, existing Stripe
   usage (Payment Links, Checkout, Netlify functions?), design system (fonts, colours, header/footer).
2. **Decide where the Python backend runs.** Netlify can't run a long-lived Python worker + SQLite. Options:
   a small server (Render, Railway, Fly.io, a VPS) at e.g. `audit-api.reelkix.com`, or inside the existing
   "Recovery Engine" at app.reelkix.com if that's a server (open decision in the spec). Ask the owner.
3. **Connect the site to the backend.** Either:
   - a Netlify proxy so `reelkix.com/audit/*` serves the backend (see the `netlify.toml` snippet in README), or
   - native pages in the Reelkix site that link/post to the backend (`POST /checkout` with `tier`, `email`, `ref`).
   Restyle the backend's Jinja templates (`creator_audit/templates/`) to match the Reelkix look.
4. **Stripe (existing account):**
   - Put the secret key in the backend's `.env` as `STRIPE_SECRET_KEY`. The owner should paste it into the
     file or the host's environment settings themselves, **never into chat**.
   - Stripe Dashboard → Developers → Webhooks → add endpoint `https://<backend>/stripe/webhook` with events
     `checkout.session.completed` and `charge.refunded`; put the signing secret in `STRIPE_WEBHOOK_SECRET`.
   - If the site already has Stripe products/prices, decide whether to reuse them; the backend currently
     creates prices inline (`price_data`) in `creator_audit/app.py` → `checkout()`.
5. **Remove postcards** (owner decision): delete the Lob/postcard code paths in `creator_audit/local.py`
   (`send_postcards`, `_send_postcard`, `postcard_html`, `_postcard_spend_this_month`), the `/qr` route and
   `segno` if unused, the postcard settings in `config.py` / `.env.example`, the postcard parts of
   `admin_local.html` and the README, and update `tests/test_local.py`.
6. **Add a self-serve "Free website preview" page** (proposed, owner to confirm): a business types its name +
   city → backend looks it up with Places Text Search → creates the business row → redirects to `/b/<token>`
   with the check-up, mock site and $200 button. That makes the local side fully online and hands-off
   (traffic from ads/social instead of cold contact). Rate-limit it; each lookup costs a Places call.
7. **Keys the owner must create and put in `.env` (not in chat):** `GOOGLE_PLACES_API_KEY` (Google Cloud, Places
   API New), `YOUTUBE_API_KEY` (YouTube Data API v3), `ANTHROPIC_API_KEY`, SMTP credentials for the sending
   domain (with SPF/DKIM/DMARC), `ADMIN_PASSWORD`, `BASE_URL`.
8. **Before launch:** confirm Google Maps terms (mock pages built from Places data, short in-memory cache),
   YouTube data-retention window, privacy/terms pages reviewed by a lawyer.

## 4. Decisions already made / still open

Made: online only, no postcards; website price $200; existing Stripe account; Standard audit $19.99.

Open: product name, Premium price, keep the $4.99 unlock, where the backend is hosted, YouTube niches and
subscriber range, local categories and cities, paywall vs "book a call" for websites, mock expiry
(default 30 days), whether to build the self-serve preview page.

## 5. Things to know about the code

- Config is all environment variables: `creator_audit/config.py`, documented in `.env.example`. A blank key
  falls back to an offline/dev mode, e.g. no `STRIPE_SECRET_KEY` means a fake checkout that marks orders paid.
- One process: `python -m creator_audit all` (web + worker + scheduler). They can also run separately.
- Only the worker calls Claude (`claude-opus-5-5`, structured output); `narrative.py` + `numcheck.py`.
- Local businesses: only Google place IDs are stored; details are re-fetched (`places.py`, `local.py`).
- Do-not-contact list is checked before every send; every outreach email has unsubscribe + business address.
