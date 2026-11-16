"""Premium plans: a 30-day "fix" plan and a 60-day "grow" plan (days 31-60), built by rules from the
creator's own audit (top problems, best videos, format mix) and goals (posting capacity, what to grow).

Deterministic and free (no AI). Text avoids writing digits except inside quoted video titles, so it
passes the same number check as everything else in the report. Each entry: day, format, theme, idea.
"""
from __future__ import annotations

import re

ORDINAL = ["one", "two", "three", "four", "five", "six", "seven", "eight"]

# posting slots per week as (weekday offset 0-6, format); "main" = the channel's stronger format
CAPACITY = {
    "1 video a week": [(1, "main")],
    "2 videos a week": [(1, "main"), (4, "main")],
    "3+ videos a week": [(0, "main"), (2, "short"), (4, "main")],
    "1 long-form + 3 Shorts a week": [(1, "long-form"), (2, "short"), (4, "short"), (6, "short")],
    "Daily Shorts": [(d, "short") for d in range(7)],
}
CAPACITY_WORDS = {
    "1 video a week": "one video a week",
    "2 videos a week": "two videos a week",
    "3+ videos a week": "three uploads a week",
    "1 long-form + 3 Shorts a week": "one long-form video and three Shorts a week",
    "Daily Shorts": "a Short every day",
}

FIX_ACTION = {
    "cadence": ("Stay consistent", "Batch-film this with your next upload so one finished video is always in reserve."),
    "packaging": ("Win the click", "Make two thumbnail options and run YouTube's Test & Compare; lead the title with the payoff."),
    "retention_long": ("Keep them watching", "Open on the payoff, keep the intro very short, and cut every section that doesn't move the video forward."),
    "retention_short": ("Stop the swipe", "Start on the strongest frame with no lead-in, and loop the ending back to the opening."),
    "conversion": ("Turn viewers into subscribers", "Ask for the subscribe right after the main payoff and point the end screen at your series playlist."),
    "format_shorts": ("Lean into Shorts", "Cut a Short from your latest long-form video and point it at the full video."),
    "format_long": ("Lean into long-form", "Expand one of your best Shorts into a full video that goes deeper."),
    "content_direction": ("Go back to what works", "Pick the topic from your best performers, not a brand-new experiment."),
}
GOAL_PUSH = {
    "Subscribers": ("Subscriber push", "Make it an episode of a named series and tell viewers to subscribe for the next one."),
    "Views": ("Views push", "Post a Short the day before that teases this video's payoff and links to it."),
    "Watch time / monetization": ("Watch-time push", "Make this a deeper, longer video and end-screen into a playlist of related videos."),
    "Sponsorship-ready audience": ("Sponsor-ready push", "Keep it squarely in your niche and plan a natural spot a sponsor segment could fit."),
    "Sales of my own product": ("Sales push", "Show your product solving the problem in this video and pin the link in the comments."),
}
FORMAT_NOTE = {
    "long-form": "Long-form",
    "short": "Short",
    "community post": "Community post",
    "review": "Check-in",
}


def _action_for(problem: dict, metrics: dict) -> tuple[str, str]:
    key, ft = problem.get("key", ""), problem.get("fix_type", "")
    if key == "low_shorts_retention":
        return FIX_ACTION["retention_short"]
    if key == "format_mismatch_shorts":
        return FIX_ACTION["format_shorts"]
    if key == "format_mismatch_longform":
        return FIX_ACTION["format_long"]
    if ft == "retention":
        return FIX_ACTION["retention_long"]
    if ft == "format":
        fm = metrics.get("format_mix", {})
        better_shorts = (fm.get("shorts_views_per_upload") or 0) > (fm.get("longform_views_per_upload") or 0)
        return FIX_ACTION["format_shorts" if better_shorts else "format_long"]
    return FIX_ACTION.get(ft, FIX_ACTION["content_direction"])


def _main_format(metrics: dict, problems: list[dict]) -> str:
    keys = {p.get("key") for p in problems}
    if "format_mismatch_shorts" in keys:
        return "short"
    if "format_mismatch_longform" in keys:
        return "long-form"
    fm = metrics.get("format_mix", {})
    return "short" if (fm.get("shorts_share_pct") or 0) >= 70 else "long-form"


def _topics(metrics: dict, niche: str) -> list[str]:
    """Rotating sources for video ideas, taken from the creator's own best videos."""
    out = []
    for v in metrics.get("outliers", {}).get("top", [])[:3]:
        out.append(f'Revisit the topic of "{v["title"]}" with a fresh angle.')
    words = metrics.get("outliers", {}).get("traits", {}).get("common_title_words") or []
    if words:
        out.append(f"Build the title around a word your hits share: {', '.join(words[:3])}.")
    subject = niche if niche and not re.search(r"\d", niche) else "your niche"
    out.append(f"Answer a question viewers keep asking about {subject}.")
    out.append(f"Make the most useful beginner video you can about {subject}.")
    return out


def _week(day_offset: int, slots, theme: str, action: str, topics: list[str], ti: list[int], main: str) -> list[dict]:
    rows = []
    for off, fmt in slots:
        fmt = main if fmt == "main" else fmt
        idea = f"{topics[ti[0] % len(topics)]} {action}"
        ti[0] += 1
        rows.append({"day": day_offset + off + 1, "format": fmt, "theme": theme, "idea": idea})
    return rows


def build(metrics: dict, problems: list[dict], goals: dict | None) -> dict:
    goals = goals or {}
    capacity = goals.get("posting_capacity") or "2 videos a week"
    slots = CAPACITY.get(capacity, CAPACITY["2 videos a week"])
    goal = goals.get("goal") or "Views"
    main = _main_format(metrics, problems)
    topics = _topics(metrics, (goals.get("niche") or "").strip())
    ti = [0]
    # one distinct focus per week, in problem order (two packaging problems -> one packaging week)
    focus: list[tuple[str, str]] = []
    for p in problems:
        act = _action_for(p, metrics)
        if act not in focus and act != FIX_ACTION["content_direction"]:
            focus.append(act)
    for extra in ("cadence", "packaging", "retention_long", "conversion"):  # fill up when few problems
        if len(focus) >= 3:
            break
        if FIX_ACTION[extra] not in focus:
            focus.append(FIX_ACTION[extra])

    # ---- days 1-30: fix the top problems, then double down
    plan30: list[dict] = []
    for w in range(4):
        if w < 3:
            theme, action = focus[w]
            theme = f"Week {ORDINAL[w]}: {theme}"
        else:
            theme, action = f"Week {ORDINAL[w]}: Double down", FIX_ACTION["content_direction"][1]
        plan30 += _week(w * 7, slots, theme, action, topics, ti, main)
    plan30 = [r for r in plan30 if r["day"] <= 29]
    plan30.append({"day": 30, "format": "review", "theme": "Check-in",
                   "idea": "In YouTube Studio, compare your latest uploads' click-through rate and average percentage viewed "
                           "with this report. Keep what moved, drop what didn't."})

    # ---- days 31-60: grow
    other = "short" if main == "long-form" else "long-form"
    gtheme, gaction = GOAL_PUSH.get(goal, GOAL_PUSH["Views"])
    weeks60 = [
        ("Week five: Start a series", "Make it an episode of a named series built on your best-performing topic, with the same title style each time."),
        (f"Week six: {gtheme}", gaction),
        ("Week seven: Format experiment", f"Try this one as a {FORMAT_NOTE[other].lower()} and compare it with your usual format."),
        ("Week eight: Lock in the habit", "Keep the exact posting days you've held for the last weeks; consistency is the goal now."),
    ]
    plan60: list[dict] = []
    for w, (theme, action) in enumerate(weeks60):
        rows = _week(30 + w * 7, slots, theme, action, topics, ti, main)
        if w == 2 and rows:  # the experiment week actually switches format once
            rows[0]["format"] = other
        plan60 += rows
    plan60 = [r for r in plan60 if r["day"] <= 59 and r["day"] != 45]
    plan60.append({"day": 45, "format": "review", "theme": "Mid-point check-in",
                   "idea": "Compare the series episodes with your other uploads. If the series is ahead, give it the next slot too."})
    plan60.append({"day": 60, "format": "review", "theme": "Re-audit",
                   "idea": "Run a fresh audit: we email you a link today. It shows your new score next to this one."})
    plan60.sort(key=lambda r: r["day"])

    cap_words = CAPACITY_WORDS.get(capacity, "a steady rhythm")
    return {
        "plan_intro": (f"Your first month fixes the problems costing you the most, one week at a time, then doubles down on what "
                       f"already works. Posting rhythm: {cap_words}, aimed at {goal.lower()}."),
        "schedule": plan30,
        "plan60_intro": ("Days thirty-one to sixty turn those fixes into growth: a series built on your best topic, a push "
                         f"for {goal.lower()}, one format experiment, and a re-audit at the end so you can see what changed."),
        "schedule60": plan60,
    }


def title_checks(metrics: dict) -> list[dict]:
    """Rule-based title and thumbnail fixes for recent uploads (used when no AI writer is set)."""
    traits = metrics.get("outliers", {}).get("traits", {})
    hit_words = traits.get("outlier_median_title_words")
    common = traits.get("common_title_words") or []
    avg_ctr = (metrics.get("packaging") or {}).get("avg_ctr_pct")
    out = []
    for v in metrics.get("recent_uploads", []):
        n = len(v["title"].split())
        tips = []
        if hit_words and n > hit_words + 2:
            tips.append("It's longer than the titles of your best videos; cut it down to the payoff.")
        elif hit_words and n < hit_words - 2:
            tips.append("It's shorter than the titles of your best videos; add what the viewer gets.")
        if common and not any(w.lower() in v["title"].lower() for w in common):
            tips.append(f"Your hits tend to use words like {', '.join(common[:2])}; work one in if it fits.")
        if avg_ctr and v.get("ctr_pct") is not None and v["ctr_pct"] < avg_ctr:
            tips.append("Its click-through is below your channel average, so the thumbnail is the first thing to change.")
        if not tips:
            tips.append("Lead with the result the viewer gets, then the topic.")
        out.append({"video_id": v["video_id"], "new_title": "",
                    "thumbnail_idea": "One clear subject, a short caption of a few words, high contrast so it reads at phone size.",
                    "why": " ".join(tips)})
    return out
