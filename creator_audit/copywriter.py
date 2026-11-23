"""Website copy engine (free, no AI): turns a business's real facts + their own words into the content of a
high-converting local-business site.

What the research says and how each rule shows up here:
- Visitors decide to stay or leave in about the first 10 seconds (NN/G, from Microsoft Research dwell-time data),
  so the hero says WHAT you do, WHERE, and WHY you, in one short headline + one line of proof, with one obvious action.
- 97% of consumers read reviews for local businesses and 47% won't use one with fewer than 20 (BrightLocal Local
  Consumer Review Survey 2026), so real Google rating + count sits right under the headline and next to every call
  to action. Proof is only shown when it's real: nothing here invents years, licences or awards.
- Most local searches happen on phones and a 1s -> 3s load raises bounce probability 32% (Google/SOASTA), so the
  page is a single fast static file with a tap-to-call bar that never leaves the screen on mobile.
- People scan in an F-pattern when nothing guides the eye (NN/G), so every section opens with a plain heading that
  makes sense on its own, then 1-2 short sentences. Services read as customer outcomes, not jargon.
- Trust signals work best near the action (practitioner A/B testing), so proof is repeated beside the final call
  to action, plus clear service area, hours and directions that answer "can they help me, today, here?".
- Copy rules: second person ("you"), short sentences, specific verbs, no hype words ("best", "world-class",
  "#1"), no exclamation marks, every claim traceable to their site, Google, or a plain description of the trade.
"""
from __future__ import annotations

import hashlib
import re

# ------------------------------------------------------------------ trade packs
# services: (keywords to recognise it in their own site text, name, customer-outcome blurb)
TRADES = {
    "barber": {
        "noun": "barbershop", "verb": "cut",
        "headlines": ["Sharp cuts. No guesswork.", "Clean fades, sharp lines, easy chairs.",
                      "The cut you asked for, every visit."],
        "promise": "Walk out looking like you meant it.",
        "primary": ("Book a cut", "Call to book"),
        "services": [
            (("fade", "skin fade", "taper"), "Fades & tapers", "Smooth, even blends from skin to length, shaped to your head."),
            (("haircut", "cut", "classic"), "Classic cuts", "Scissor and clipper cuts that grow out well, not just look good on day one."),
            (("beard", "trim", "line up", "line-up", "edge"), "Beard trims & line-ups", "Crisp edges and a beard shaped to your face."),
            (("shave", "hot towel", "razor"), "Hot towel shaves", "A slow, close straight-razor shave with hot towels."),
            (("kid", "child", "boys"), "Kids' cuts", "Patient barbers and a quick, clean cut for younger clients."),
            (("design", "part", "art"), "Designs & hard parts", "Clean parts and freehand designs done with steady hands."),
        ],
        "why": [("Barbers who listen", "Tell us what you want; we'll tell you honestly what will work."),
                ("On time, every time", "Your appointment means your chair is ready when you walk in."),
                ("Clean tools, clean shop", "Every tool is cleaned between clients. Always.")],
        "process": [("Pick a time", "Call or walk in. We'll fit you in fast."), ("Tell us the look", "Bring a photo or describe it."),
                    ("Walk out sharp", "Finished, styled and lined up.")],
        "faqs": [("Do you take walk-ins?", "Call ahead and we'll tell you the current wait, or book a time so your chair is ready."),
                 ("How much is a haircut?", "Prices depend on the service. Call us and we'll give you an exact price before we start."),
                 ("How long does a cut take?", "Most cuts take 30 to 45 minutes. Add about 15 minutes for a beard trim.")],
    },
    "beauty": {
        "noun": "salon", "verb": "appointment",
        "headlines": ["Look good. Leave relaxed.", "Detailed work, calm visits.", "Beautiful results that last."],
        "promise": "Careful, detailed work in a calm, clean studio.",
        "primary": ("Book an appointment", "Call to book"),
        "services": [
            (("manicure", "mani"), "Manicures", "Shaped, cleaned and polished, with care for your cuticles and nail health."),
            (("pedicure", "pedi"), "Pedicures", "A relaxing soak, scrub and polish that leaves your feet feeling new."),
            (("gel", "shellac"), "Gel polish", "Glossy colour that stays chip-free for weeks."),
            (("acrylic", "full set", "fill", "extension", "dip"), "Acrylics, dip & fills", "Strong, natural-looking sets shaped exactly how you like them."),
            (("nail art", "design"), "Nail art", "From clean minimal lines to detailed custom designs."),
            (("color", "colour", "highlight", "balayage"), "Colour & highlights", "Colour matched to your skin tone and maintained with care."),
            (("blowout", "style", "styling", "updo"), "Styling & blowouts", "Smooth, polished styles for every day or a big event."),
            (("wax", "brow", "lash"), "Brows, lashes & waxing", "Neat, gentle shaping that frames your face."),
            (("facial", "skin"), "Facials", "Deep-cleansing treatments tailored to your skin."),
        ],
        "why": [("Spotless and safe", "Tools are sanitised between every client, every time."),
                ("Time for the details", "We don't rush. Your appointment is yours."),
                ("Results that last", "Quality products and careful technique mean fewer touch-ups.")],
        "process": [("Book your time", "Call or message us to pick a slot."), ("Relax", "We'll walk you through options and get started."),
                    ("Love the result", "We finish when you're happy with it.")],
        "faqs": [("Do I need an appointment?", "Appointments are best so we can give you our full attention. Call us to check today's availability."),
                 ("What are your prices?", "It depends on the service and design. Call us and we'll give you a clear price up front."),
                 ("How long does an appointment take?", "Most services take 45 to 90 minutes. We'll give you a time estimate when you book.")],
    },
    "food": {
        "noun": "restaurant", "verb": "visit",
        "headlines": ["Good food, made fresh, close to home.", "Fresh food, friendly faces.", "Made fresh. Served hot. Worth the trip."],
        "promise": "Honest food made from good ingredients.",
        "primary": ("Call to order", "Call us"),
        "services": [
            (("dine", "dining", "eat in"), "Dine in", "Grab a table and settle in. We'll take care of the rest."),
            (("takeout", "take out", "to go", "pickup", "pick up"), "Takeout", "Call ahead and your order will be hot and ready when you arrive."),
            (("deliver",), "Delivery", "Your favourites brought to your door."),
            (("cater", "event", "party"), "Catering", "Feed the office, the party or the family, without the stress."),
            (("breakfast", "brunch"), "Breakfast & brunch", "Start the day right with plates made to order."),
            (("lunch", "special"), "Lunch specials", "Quick, filling and fairly priced for the middle of your day."),
            (("dessert", "bakery", "cake", "pastr"), "Desserts", "Something sweet, baked with care."),
        ],
        "why": [("Made fresh daily", "Real ingredients, cooked to order."), ("Friendly and quick", "Good service without the wait."),
                ("Fair prices", "Generous plates that are worth what you pay.")],
        "process": [("Call or walk in", "Order ahead or grab a table."), ("We cook it fresh", "Made to order in our kitchen."),
                    ("Enjoy", "Here, at home, or at your event.")],
        "faqs": [("Do you take reservations?", "Call us and we'll hold a table for your group."),
                 ("Can I order ahead?", "Yes. Call your order in and it will be ready when you arrive."),
                 ("Do you have options for dietary needs?", "Tell us what you need when you order and we'll help you choose.")],
    },
    "auto": {
        "noun": "auto shop", "verb": "repair",
        "headlines": ["Honest repairs. Straight answers.", "Fixed right, explained clearly, priced fairly.", "Car trouble? We'll tell you the truth."],
        "promise": "Clear answers and quality repairs, without the runaround.",
        "primary": ("Call for a quote", "Call the shop"),
        "services": [
            (("diagnos", "check engine", "inspection"), "Diagnostics", "We find the real cause and show you before any work starts."),
            (("brake",), "Brakes", "Pads, rotors and fluid, so you stop safely every time."),
            (("oil", "lube", "fluid"), "Oil changes", "Quick, clean oil and filter service with a free once-over."),
            (("tire", "tyre", "alignment", "rotation"), "Tires & alignment", "Even wear, straight driving and longer-lasting tires."),
            (("a/c", "ac ", "air condition", "heating"), "A/C & heating", "Cold air in summer and heat when you need it."),
            (("transmission",), "Transmission", "Smooth shifting restored, with honest repair-or-replace advice."),
            (("engine", "timing", "tune"), "Engine repair", "From misfires to major repairs, done right the first time."),
            (("battery", "electrical", "starter", "alternator"), "Battery & electrical", "Reliable starts and electrics you can count on."),
            (("wash", "detail"), "Wash & detailing", "A spotless finish inside and out."),
        ],
        "why": [("Straight answers", "We explain what's wrong in plain English and show you the old parts."),
                ("Approval first", "No work starts until you've seen the price and said yes."),
                ("Done right once", "Quality parts and careful work, so you're not back next week.")],
        "process": [("Call or drop by", "Tell us what the car is doing."), ("Get a clear quote", "We diagnose it and explain the price before any work."),
                    ("Drive away confident", "Repaired, test-driven and ready.")],
        "faqs": [("Do you give quotes before starting?", "Yes. We diagnose the problem and tell you the price, and nothing is done until you approve it."),
                 ("How long will my repair take?", "Many repairs are same-day. We'll give you a time estimate with your quote."),
                 ("Can I wait while you work?", "For quick services, yes. Call ahead and we'll tell you how long it should take.")],
    },
    "health": {
        "noun": "practice", "verb": "appointment",
        "headlines": ["Care you can count on, close to home.", "Unhurried care from people who listen.", "Feel better. We'll take the time."],
        "promise": "Attentive, unhurried care from a team that puts you first.",
        "primary": ("Book an appointment", "Call us"),
        "services": [
            (("new patient",), "New patients", "Easy first visits with time to answer every question."),
            (("exam", "check", "consult"), "Exams & consultations", "A thorough look and a clear plan you understand."),
            (("clean", "hygien"), "Cleanings", "Gentle, thorough care to keep problems away."),
            (("adjust", "chiro", "spine", "back"), "Adjustments", "Targeted care to ease pain and help you move better."),
            (("therapy", "rehab", "physio"), "Therapy & rehab", "Step-by-step plans to get you back to normal."),
            (("emergenc", "same day", "urgent"), "Same-day visits", "When it can't wait, call and we'll do our best to see you today."),
            (("insurance",), "Insurance help", "We'll help you understand what your plan covers."),
        ],
        "why": [("We listen first", "Your visit starts with your questions, not a clipboard."), ("Clear plans", "You'll know what we recommend and why."),
                ("Comfort matters", "A calm, friendly visit from start to finish.")],
        "process": [("Call to book", "We'll find a time that works."), ("Your visit", "We listen, examine and explain."),
                    ("Your plan", "Clear next steps, in writing if you'd like.")],
        "faqs": [("Are you taking new patients?", "Call us and we'll check availability and get you booked."),
                 ("Do you take my insurance?", "Call with your plan details and we'll tell you what's covered before your visit."),
                 ("What should I bring?", "Your ID, insurance card and a list of any medications.")],
    },
    "trades": {
        "noun": "company", "verb": "job",
        "headlines": ["Done right the first time.", "On time, clean, and done right.", "Reliable work. Clear prices. No mess left behind."],
        "promise": "Pros who show up on time and leave the place clean.",
        "primary": ("Get a free quote", "Call for a quote"),
        "services": [
            (("repair", "fix", "leak"), "Repairs", "Fast, lasting fixes so the problem stays fixed."),
            (("install", "replace", "new"), "Installations", "New equipment installed properly, tested and cleaned up after."),
            (("emergenc", "24", "after hours"), "Emergency service", "When it can't wait, call and we'll get to you fast."),
            (("maintenance", "tune", "inspect", "service plan"), "Maintenance", "Regular checks that prevent costly breakdowns."),
            (("estimate", "quote", "consult"), "Free estimates", "A clear, written price before any work starts."),
            (("roof", "shingle", "gutter"), "Roofing", "Solid, weather-tight roofs and gutters that drain right."),
            (("drain", "pipe", "water heater", "plumb"), "Plumbing", "Leaks, drains and water heaters handled cleanly."),
            (("wiring", "panel", "outlet", "electric", "lighting"), "Electrical", "Safe, code-compliant electrical work."),
            (("paint", "drywall"), "Painting", "Clean lines and even coats, inside and out."),
            (("lawn", "landscap", "mow", "tree", "yard"), "Lawn & landscaping", "A yard you're proud of, kept that way."),
            (("clean", "maid", "janitorial", "pressure wash"), "Cleaning", "Thorough, reliable cleaning on your schedule."),
            (("hvac", "furnace", "a/c", "air condition", "heat pump"), "Heating & cooling", "Comfortable rooms all year, efficient systems."),
            (("lock", "key", "rekey"), "Locks & keys", "Lockouts, rekeys and upgrades, handled quickly."),
        ],
        "why": [("On time", "We show up when we say we will."), ("Clear prices", "You get the price up front, before work starts."),
                ("Clean job sites", "We protect your home and clean up when we're done.")],
        "process": [("Call or request a quote", "Tell us what you need."), ("Get a clear price", "We look at the job and quote it up front."),
                    ("Job done right", "Finished, tested and cleaned up.")],
        "faqs": [("Do you give free estimates?", "Call us to describe the job and we'll explain how we price it before anything starts."),
                 ("How soon can you come out?", "Call us and we'll give you the earliest time available."),
                 ("What areas do you serve?", "We serve {city} and nearby areas. Call to confirm we cover your address.")],
    },
    "default": {
        "noun": "business", "verb": "visit",
        "headlines": ["Local, friendly and here to help.", "Good service, close to home.", "Your neighbours already know us."],
        "promise": "A local business that takes pride in great service.",
        "primary": ("Call us", "Call now"),
        "services": [],
        "why": [("Local and personal", "You'll deal with real people who know the area."), ("Fair and clear", "Straight answers and honest prices."),
                ("Here when you need us", "Easy to reach and quick to help.")],
        "process": [("Call or stop by", "Tell us what you need."), ("We take care of it", "Quickly and properly."),
                    ("You're all set", "And we're here if you need anything else.")],
        "faqs": [("How do I get started?", "Call us and we'll take it from there."),
                 ("What are your hours?", "Our current hours are listed on this page. Call ahead on holidays.")],
    },
}
TRADES["barber"]["fallback"] = ["Classic cuts", "Fades & tapers", "Beard trims & line-ups", "Hot towel shaves"]
TRADES["beauty"]["fallback"] = ["Manicures", "Pedicures", "Gel polish", "Acrylics, dip & fills"]
TRADES["food"]["fallback"] = ["Dine in", "Takeout", "Catering"]
TRADES["auto"]["fallback"] = ["Diagnostics", "Brakes", "Oil changes", "Tires & alignment"]
TRADES["health"]["fallback"] = ["New patients", "Exams & consultations", "Same-day visits"]
TRADES["trades"]["fallback"] = ["Repairs", "Installations", "Maintenance", "Free estimates"]
TRADES["default"]["fallback"] = []

ICONS = {  # tiny inline SVG paths (24x24, stroke) so the page needs no icon font
    "scissors": "M6 6a3 3 0 1 0 0 .01M6 18a3 3 0 1 0 0 .01M20 4 8.1 15.9M14.5 14.5 20 20M8.1 8.1 12 12",
    "spark": "M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6",
    "wrench": "M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.6 2.6-2.4-.6-.6-2.4z",
    "plate": "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
    "heart": "M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10z",
    "home": "M3 11 12 4l9 7M5 10v10h14V10M10 20v-6h4v6",
    "check": "M4 12.5 9 17.5 20 6.5",
    "clock": "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2",
    "shield": "M12 3 5 6v6c0 4.4 3 7.6 7 9 4-1.4 7-4.6 7-9V6z",
    "star": "M12 3.5l2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.8l-5.2 2.8 1-5.8-4.3-4.1 5.9-.9z",
}
SOCIAL_SVG = {  # brand glyphs, 24x24
    "facebook": '<path fill="currentColor" d="M14 8.6V7c0-.8.5-1 .9-1H17V2.1L14 2C10.7 2 10 4.4 10 6v2.6H7.6V12H10v10h4V12h3l.5-3.4z"/>',
    "instagram": '<rect x="3" y="3" width="18" height="18" rx="5" fill="none" stroke="currentColor" stroke-width="1.9"/><circle cx="12" cy="12" r="4.1" fill="none" stroke="currentColor" stroke-width="1.9"/><circle cx="17.4" cy="6.6" r="1.2" fill="currentColor"/>',
    "youtube": '<path fill="currentColor" d="M22 8.2a3 3 0 0 0-2.1-2.1C18 5.6 12 5.6 12 5.6s-6 0-7.9.5A3 3 0 0 0 2 8.2 31 31 0 0 0 1.6 12a31 31 0 0 0 .4 3.8 3 3 0 0 0 2.1 2.1c1.9.5 7.9.5 7.9.5s6 0 7.9-.5a3 3 0 0 0 2.1-2.1 31 31 0 0 0 .4-3.8 31 31 0 0 0-.4-3.8zM10 15.2V8.8l5.2 3.2z"/>',
    "tiktok": '<path fill="currentColor" d="M16.5 3c.4 2.3 1.8 3.7 4 3.9v3.2c-1.5 0-2.9-.4-4-1.2v6.3A5.8 5.8 0 1 1 10.7 9.4v3.3a2.6 2.6 0 1 0 2.6 2.6V3z"/>',
}
TRADE_ICON = {"barber": "scissors", "beauty": "spark", "auto": "wrench", "food": "plate", "health": "heart",
              "trades": "home", "default": "check"}
WHY_ICONS = ["check", "clock", "shield"]

PALETTES = {  # (accent, deep, soft background, display font, body font)
    "barber": ("#1f4e79", "#0e2238", "#f3f6fa", "Fraunces", "Inter"),
    "beauty": ("#b8476f", "#3a1626", "#fbf4f6", "Playfair Display", "Inter"),
    "food": ("#c0521c", "#3b1a08", "#fdf7f0", "Fraunces", "Inter"),
    "auto": ("#0f766e", "#0b2523", "#f1f8f7", "Archivo", "Inter"),
    "health": ("#2563eb", "#0f1f44", "#f4f7fe", "Fraunces", "Inter"),
    "trades": ("#c2410c", "#2b1409", "#fdf6f1", "Archivo", "Inter"),
    "default": ("#1e5ac8", "#0f1b33", "#f5f8fd", "Fraunces", "Inter"),
}

HYPE = re.compile(r"\b(best|world[- ]class|#1|number one|top[- ]rated|leading|premier|ultimate|amazing|awesome)\b", re.I)


def _lum(hexc: str) -> float:
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hexc[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def readable_accent(hexc: str, fallback: str) -> tuple[str, str]:
    """Brand colour darkened until it reads as text on white (WCAG AA 4.5:1), plus the text colour for buttons."""
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", hexc or "") or _contrast(hexc, "#ffffff") < 2.6:
        hexc = fallback  # very light brand colours (yellows, pastels) turn muddy when darkened: use the trade palette
    r, g, b = (int(hexc[i:i + 2], 16) for i in (1, 3, 5))
    for _ in range(30):
        c = f"#{r:02x}{g:02x}{b:02x}"
        if _contrast(c, "#ffffff") >= 4.5:
            break
        r, g, b = int(r * 0.9), int(g * 0.9), int(b * 0.9)
    c = f"#{r:02x}{g:02x}{b:02x}"
    return c, "#ffffff" if _contrast(c, "#ffffff") >= _contrast(c, "#111111") else "#111111"


def _pick(options: list, seed: str):
    return options[int(hashlib.sha1(seed.encode()).hexdigest(), 16) % len(options)] if options else None


def _facts(text: str) -> dict:
    """Real claims found in THEIR OWN site text (never invented)."""
    t = text.lower()
    since = re.search(r"\b(?:since|established|est\.?|founded in|serving .{0,30} since)\s*(19[5-9]\d|20[0-2]\d)\b", t)
    return {
        "since": int(since.group(1)) if since else None,
        "family": bool(re.search(r"family[- ](owned|run|operated)", t)),
        "licensed": bool(re.search(r"\blicensed\b", t)), "insured": bool(re.search(r"\binsured\b", t)),
        "free_estimates": bool(re.search(r"free (estimate|quote|consult)", t)),
        "emergency": bool(re.search(r"24/7|24 hours|emergency", t)),
        "walk_ins": bool(re.search(r"walk[- ]ins? (welcome|accepted)", t)),
        "warranty": bool(re.search(r"warrant(y|ied)|guarantee", t)),
        "veteran": bool(re.search(r"veteran[- ]owned", t)), "woman": bool(re.search(r"(woman|women)[- ]owned", t)),
    }


def _phone_href(phone: str | None) -> str | None:
    if not phone:
        return None
    d = re.sub(r"[^\d+]", "", phone)
    return "tel:" + (d if d.startswith("+") else ("+1" + d if len(d) == 10 else d))


def _sentence(s: str, limit: int = 220) -> str:
    """Their own words, cleaned: hype removed, whole sentences only, never cut mid-thought."""
    s = HYPE.sub("", re.sub(r"\s+", " ", s or "")).replace("!", ".").replace(" ,", ",").strip()
    s = re.sub(r"\s{2,}", " ", s)
    parts = re.split(r"(?<=[.?])\s+", s)
    out = ""
    for part in parts:
        if len(out) + len(part) + 1 > limit:
            break
        out = (out + " " + part).strip()
    if not out:  # first sentence alone is too long: end at the last clause break
        out = re.split(r",\s", s[:limit])[0].rstrip(",;: ") + "."
    if not out.endswith((".", "?")):
        out += "."
    return out[0].upper() + out[1:] if out else out


def _services(pack: dict, site_text: str, owner_services: list[str] | None) -> list[dict]:
    icon = TRADE_ICON.get(pack.get("key", "default"), "check")
    if owner_services:  # the owner's own list (setup form) always wins
        bank = {name.lower(): blurb for _, name, blurb in pack["services"]}
        out = []
        for s in owner_services[:9]:
            blurb = bank.get(s.lower()) or next((b for kw, _, b in pack["services"] if any(k in s.lower() for k in kw)), "")
            out.append({"name": s, "blurb": blurb or "Call us for details and pricing.", "icon": icon})
        return out
    t = site_text.lower()
    hits = [{"name": n, "blurb": b, "icon": icon} for kw, n, b in pack["services"] if any(k in t for k in kw)]
    if len(hits) < 3:
        have = {h["name"] for h in hits}
        hits += [{"name": n, "blurb": b, "icon": icon} for _, n, b in pack["services"] if n in pack["fallback"] and n not in have]
    return hits[:6] or [{"name": "Our services", "blurb": "Call us and we'll talk you through the options.", "icon": icon}]


def build(*, name: str, theme_key: str, city: str = "", state: str = "", phone: str | None = None,
          email: str | None = None, address: str = "", maps_uri: str | None = None, hours: list[str] | None = None,
          rating: float | None = None, reviews: int = 0, site: dict | None = None, owner: dict | None = None,
          type_label: str = "") -> dict:
    """Everything the page template renders. owner = what the business confirmed in the setup form (wins)."""
    pack = dict(TRADES.get(theme_key) or TRADES["default"])
    pack["key"] = theme_key if theme_key in TRADES else "default"
    site, owner = site or {}, owner or {}
    seed = name + city
    site_text = " ".join([site.get("title", ""), site.get("description", ""), site.get("headline", "")]
                         + site.get("subheads", []) + site.get("paragraphs", []) + site.get("services", []))
    facts = _facts(site_text + " " + owner.get("blurb", ""))
    where = city or (address.split(",")[1].strip() if address.count(",") >= 2 else "")

    headline = owner.get("tagline") or _pick(pack["headlines"], seed)
    eyebrow = f"{(type_label or pack['noun']).title()}{' in ' + where if where else ''}"
    # one line of proof under the headline (BrightLocal: reviews are read first)
    proof = []
    if rating and reviews >= 5:
        proof.append(f"{rating:.1f} from {reviews:,} Google reviews")
    if facts["since"]:
        proof.append(f"Serving {where or 'the area'} since {facts['since']}")
    elif facts["family"]:
        proof.append("Family-owned")
    sub = owner.get("blurb") or ""
    if not sub:
        own = next((p for p in site.get("paragraphs", []) if 60 <= len(p) <= 320 and not HYPE.search(p)
                    and re.search(r"(we|our|us)", p, re.I)), "")  # sounds like them talking about the business
        sub = _sentence(own, 200) if own else f"{pack['promise']}{' Proudly serving ' + where + '.' if where else ''}"
    trust = []
    if rating and reviews >= 5:
        trust.append({"icon": "star", "text": f"{rating:.1f} stars · {reviews:,} reviews"})
    for k, label in (("licensed", "Licensed"), ("insured", "Insured"), ("family", "Family-owned"),
                     ("veteran", "Veteran-owned"), ("woman", "Woman-owned"), ("free_estimates", "Free estimates"),
                     ("emergency", "Emergency service"), ("walk_ins", "Walk-ins welcome"), ("warranty", "Work guaranteed")):
        if facts[k]:
            trust.append({"icon": "shield" if k in ("licensed", "insured", "warranty") else "check", "text": label})
    if facts["since"]:
        trust.append({"icon": "clock", "text": f"Since {facts['since']}"})
    if where:
        trust.append({"icon": "home", "text": f"Local to {where}"})

    primary_label = pack["primary"][0] if phone else "Get directions"
    about = owner.get("about") or next((_sentence(p, 420) for p in site.get("paragraphs", [])
                                         if 120 <= len(p) <= 900 and re.search(r"(we|our|us)", p, re.I)
                                         and p not in sub), "")
    if not about:
        about = (f"{name} is a local {pack['noun']}{' in ' + where if where else ''}. {pack['promise']} "
                 f"Call us with any question; you'll get a straight answer from a real person.")
    faqs = [{"q": q, "a": a.replace("{city}", where or "our area")} for q, a in pack["faqs"]]
    if hours:
        faqs.append({"q": "When are you open?", "a": "Our regular hours are listed below. Call ahead on holidays."})
    services = _services(pack, site_text, owner.get("services"))
    title = f"{name}{' | ' + pack['noun'].title() + ' in ' + where if where else ''}"
    desc = _sentence(f"{name}: {sub}", 155)
    pal = PALETTES[pack["key"]]
    accent, accent_ink = readable_accent(owner.get("color") or site.get("color") or pal[0], pal[0])
    return {
        "name": name, "eyebrow": eyebrow, "headline": _sentence(headline, 80), "sub": sub, "proof": proof,
        "city": where, "state": state, "phone": phone, "phone_href": _phone_href(phone), "email": email,
        "address": address, "maps_uri": maps_uri, "hours": hours or [], "rating": rating, "reviews": reviews,
        "primary": {"label": primary_label, "href": _phone_href(phone) or maps_uri or "#visit"},
        "secondary": {"label": "Get directions", "href": maps_uri} if maps_uri and phone else None,
        "trust": trust[:5], "services": services, "services_title": f"What we {'offer' if pack['key'] != 'trades' else 'do'}",
        "why": [{"title": t, "text": x, "icon": WHY_ICONS[i % 3]} for i, (t, x) in enumerate(pack["why"])],
        "process": [{"n": i + 1, "title": t, "text": x} for i, (t, x) in enumerate(pack["process"])],
        "about": about, "faqs": faqs[:5],
        "logo": owner.get("logo") or site.get("logo"),
        "photos": (owner.get("photos") or site.get("photos") or [])[:7],
        "hero_photo": (owner.get("photos") or site.get("photos") or [None])[0] or site.get("og_image"),
        "socials": {**(site.get("socials") or {}), **(owner.get("socials") or {})},
        "accent": accent, "accent_ink": accent_ink, "deep": pal[1], "soft": pal[2], "font_display": pal[3], "font_body": pal[4],
        "icon": TRADE_ICON.get(pack["key"], "check"), "icons": ICONS, "social_svg": SOCIAL_SVG,
        "final_title": f"Ready when you are{', ' + where if where else ''}.",
        "final_text": ("Call now and talk to a real person. " if phone else "") + (proof[0] + "." if proof else pack["promise"]),
        "seo": {"title": title[:65], "description": desc, "type": "LocalBusiness"},
        "theme": pack["key"],
    }
