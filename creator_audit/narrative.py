"""Step 3: Claude writes the explanation — never the numbers.

Claude gets the metrics JSON + the ranked problem list and returns structured JSON.
Without ANTHROPIC_API_KEY a deterministic template writer is used instead (handy for
tests); its output goes through the same number check.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import config

SYSTEM = f"""You write YouTube channel audits for {config.BRAND}. Your reader is the creator: write plainly,
directly and warmly, like a sharp friend who edits videos for a living. No hype, no filler, no emojis.

Hard rules about numbers (an automatic checker rejects your text if you break them):
- Use ONLY numbers that appear in the METRICS or PROBLEMS JSON you are given. Copy them exactly
  as written there; you may add thousands separators, a % sign or an "x" after a multiple.
- Never calculate new numbers: no differences, sums, averages, ratios, percentages or rounding of your own.
- Never abbreviate numbers (write 12,400 not 12.4K). Never invent benchmarks or industry averages.
- If you want to describe size without a number, use words ("about half", "most", "a few").
- In any free-text field that is not a video title, avoid writing numbers that are not in the data.

Every fix must be concrete and doable this week by a solo creator."""

PROBLEM_ITEM = {
    "type": "object",
    "properties": {
        "key": {"type": "string", "description": "The problem key exactly as given in PROBLEMS"},
        "title": {"type": "string", "description": "Short headline, max 10 words"},
        "evidence": {"type": "string", "description": "1-2 sentences citing the evidence numbers"},
        "fix": {"type": "string", "description": "2-4 sentences: the specific fix"},
    },
    "required": ["key", "title", "evidence", "fix"],
    "additionalProperties": False,
}


def schema_for(tier: str) -> dict:
    props: dict = {
        "summary": {"type": "string", "description": "3-4 sentence overview of the channel's situation"},
        "problems": {"type": "array", "items": PROBLEM_ITEM},
        "mascot_script": {"type": "string", "description": "Spoken script for the mascot video, plain sentences"},
    }
    if tier in ("standard", "premium", "unlock"):
        props["best_videos"] = {"type": "string", "description": "What the best performers share and why they worked, 1-2 paragraphs"}
        props["format_breakdown"] = {"type": "string", "description": "Shorts vs long-form read-out and recommendation, 1 paragraph"}
    if tier == "premium":
        props["rewrites"] = {"type": "array", "items": {
            "type": "object",
            "properties": {
                "video_id": {"type": "string"},
                "new_title": {"type": "string"},
                "thumbnail_idea": {"type": "string"},
                "why": {"type": "string"},
            },
            "required": ["video_id", "new_title", "thumbnail_idea", "why"],
            "additionalProperties": False,
        }}
        props["plan_intro"] = {"type": "string", "description": "How the 30-day plan serves the creator's goals"}
        props["schedule"] = {"type": "array", "items": {
            "type": "object",
            "properties": {
                "day": {"type": "integer", "description": "Day 1-30"},
                "format": {"type": "string", "enum": ["short", "long-form", "community post", "live"]},
                "idea": {"type": "string"},
            },
            "required": ["day", "format", "idea"],
            "additionalProperties": False,
        }}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


TIER_BRIEF = {
    "teaser": "This is a FREE TEASER built from public data only. Write a 2-sentence summary and exactly 1 problem "
              "(the first one in PROBLEMS). The mascot_script is a 20-30 second hook that names the one problem and "
              "says the full audit shows the rest. Do not mention prices.",
    "unlock": "Full public-data audit (no Studio analytics: there is no click-through, retention or subscriber data, "
              "so don't discuss those). Write up to 5 problems from PROBLEMS in the given order. mascot_script: 45-60 seconds.",
    "standard": "Standard audit. Write exactly 5 problems, using the first 5 in PROBLEMS in the given order. "
                "mascot_script: 45-60 seconds summarising the score, the biggest problem and the first fix.",
    "premium": "Premium audit. Write exactly 5 problems (first 5 in PROBLEMS, in order). Rewrite titles and give a "
               "thumbnail idea for each video in RECENT_UPLOADS. Build a 30-day schedule (one entry per posting day, "
               "matching the creator's stated posting capacity) aimed at their goals. mascot_script: a 90-120 second "
               "walkthrough of the score, top problems and the plan.",
}


@dataclass
class Narrative:
    data: dict
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    notes: list[str] = field(default_factory=list)


def _user_prompt(tier, metrics, subs, score, probs, goals, feedback) -> str:
    n = 1 if tier == "teaser" else 5
    payload = {
        "TIER": tier,
        "SCORE": score,
        "SUBSCORES": subs,
        "METRICS": {k: v for k, v in metrics.items() if k != "recent_uploads"},
        "PROBLEMS": [{"key": p["key"], "fix_type": p["fix_type"], "evidence": p["evidence"]} for p in probs[:n]],
    }
    if tier == "premium":
        payload["RECENT_UPLOADS"] = metrics.get("recent_uploads", [])
        payload["GOALS"] = goals or {}
    text = TIER_BRIEF[tier] + "\n\n" + json.dumps(payload, indent=1, default=str)
    if feedback:
        text += ("\n\nYOUR PREVIOUS DRAFT FAILED THE NUMBER CHECK. These numbers are not in the data: "
                 + ", ".join(sorted(set(feedback))) + ". Rewrite without them.")
    return text


def generate(tier, metrics, subs, score, probs, goals=None, feedback=None) -> Narrative:
    if config.ANTHROPIC_API_KEY:
        return _claude(tier, metrics, subs, score, probs, goals, feedback)
    return _template(tier, metrics, subs, score, probs, goals)


def _claude(tier, metrics, subs, score, probs, goals, feedback) -> Narrative:
    import anthropic

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    resp = client.beta.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM,
        output_config={"effort": config.CLAUDE_EFFORT,
                       "format": {"type": "json_schema", "schema": schema_for(tier)}},
        messages=[{"role": "user", "content": _user_prompt(tier, metrics, subs, score, probs, goals, feedback)}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined: {getattr(resp.stop_details, 'explanation', '')}")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Claude output was cut off (max_tokens)")
    text = "".join(b.text for b in resp.content if b.type == "text")
    return Narrative(json.loads(text), resp.model, resp.usage.input_tokens, resp.usage.output_tokens)


# ---------------------------------------------------------------- offline writer
def _fmt(v) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float) and not v.is_integer():
        return f"{v:,}"
    return f"{int(v):,}"


TEMPLATES = {
    "stalled": ("Your channel has gone quiet",
                "Your last upload was {days_since_last_upload} days ago, and the algorithm stops testing channels that stop posting.",
                "Post one video this week, even a short one built from footage you already have. Then lock a realistic cadence you can hold for a month."),
    "low_cadence": ("You're not posting often enough to build momentum",
                    "You uploaded {uploads_last_90_days} videos in the last 90 days, about {uploads_per_week} per week.",
                    "Pick fixed posting days and batch-film two videos per session. Fill gaps with Shorts cut from long-form videos."),
    "long_gap": ("Long gaps are resetting your reach",
                 "Your longest stretch without an upload in the last 90 days was {longest_gap_days} days.",
                 "Keep one finished video in reserve at all times so a busy week never becomes a silent month."),
    "falling_views": ("Recent videos are pulling fewer views",
                      "The median of your last 10 uploads is {recent_median_views} views versus {previous_median_views} for the 10 before, a change of {change_pct}%.",
                      "Go back to the topics and formats of your best recent videos and cut the ones that underperformed. Test two new title angles per video."),
    "low_ctr": ("Your packaging isn't earning the click",
                "Your average click-through rate is {avg_ctr_pct}%.",
                "Make thumbnails readable at phone size: one face or object, three words max, high contrast. Lead titles with the payoff, not the setup."),
    "recent_ctr_below_avg": ("Your latest thumbnails and titles are slipping",
                             "{recent_below_avg_count} of your last {recent_count} uploads are below your own average click-through rate of {avg_ctr_pct}%.",
                             "Compare your recent thumbnails side by side with your best performers and copy what those do. Use YouTube's thumbnail test on the next upload."),
    "low_longform_retention": ("Viewers leave long videos early",
                               "Long-form videos keep an average of {longform_avg_pct_viewed}% of each video watched.",
                               "Cut the intro to under 15 seconds, deliver the promised payoff early, and trim any section that doesn't move the story forward."),
    "low_shorts_retention": ("Shorts are getting swiped away",
                             "Shorts average {shorts_avg_pct_viewed}% viewed.",
                             "Start on the most interesting frame, no lead-in. Keep Shorts tight and end on a loop back to the opening."),
    "low_conversion": ("Viewers watch but don't subscribe",
                       "You gain {subs_per_1k_views} subscribers per 1,000 views.",
                       "Ask for the subscribe right after you deliver value, tell viewers what they'll get by subscribing, and link an end screen to a series."),
    "format_mismatch_shorts": ("Shorts outperform but you rarely post them",
                               "Shorts average {shorts_views_per_upload} views per upload against {longform_views_per_upload} for long-form, yet they are only {shorts_share_pct}% of uploads.",
                               "Add Shorts cut from each long-form video, and use them to point viewers back to the full video."),
    "format_mismatch_longform": ("Long-form outperforms but you post mostly Shorts",
                                 "Long-form averages {longform_views_per_upload} views per upload against {shorts_views_per_upload} for Shorts, yet long-form is only {longform_share_pct}% of uploads.",
                                 "Shift effort toward long-form and use Shorts as trailers for it."),
    "outliers_not_repeated": ("Your hits have a pattern you're not repeating",
                              "You have {count} videos above twice your median of {channel_median_views} views, and they lean toward {top_weekday} uploads.",
                              "Make a list of what your outliers share in topic, title style and length, and build your next three videos from it."),
}


def _template(tier, metrics, subs, score, probs, goals) -> Narrative:
    n = 1 if tier == "teaser" else 5
    out_probs = []
    for p in probs[:n]:
        t = TEMPLATES.get(p["key"])
        ev = {k: (_fmt(v) if not isinstance(v, (list, str)) else v) for k, v in p["evidence"].items()}
        if t:
            title, evidence, fix = t
            evidence = evidence.format(**ev)
        else:
            area = p["key"].replace("opportunity_", "")
            title = f"Room to grow: {area}"
            evidence = f"Your {area} score is {ev.get(area + '_score', 'below the rest')} out of 100."
            fix = "This is your weakest remaining area; focus here after the problems above."
        out_probs.append({"key": p["key"], "title": title, "evidence": evidence, "fix": fix})

    ch = metrics["channel"].get("title") or "Your channel"
    summary = (f"{ch} scores {score} out of 100. The biggest issue right now: {out_probs[0]['title'].lower()}. "
               if out_probs else f"{ch} scores {score} out of 100. ")
    if tier != "teaser":
        summary += "The sections below show what's working, what isn't, and the fix for each."
    data = {"summary": summary, "problems": out_probs,
            "mascot_script": f"Hey! I looked at {ch}. Your score is {score} out of 100. "
                             + (f"Here's the big one: {out_probs[0]['title'].lower()}. {out_probs[0]['fix']}" if out_probs else "")}
    if tier in ("standard", "premium", "unlock"):
        o = metrics["outliers"]
        if o["count"]:
            data["best_videos"] = (f"{o['count']} videos beat twice your median of {_fmt(o['channel_median_views'])} views. "
                                   f"Your top video, \"{o['top'][0]['title']}\", did {o['top'][0]['multiple']}x your median.")
        else:
            data["best_videos"] = "No video has broken out above twice your median yet, so the next step is testing new angles."
        fm = metrics["format_mix"]
        data["format_breakdown"] = (f"Shorts are {fm['shorts_share_pct']}% of your uploads and long-form {fm['longform_share_pct']}%. "
                                    f"Shorts average {_fmt(fm['shorts_views_per_upload'])} views per upload; long-form averages {_fmt(fm['longform_views_per_upload'])}.")
    if tier == "premium":
        data["rewrites"] = [{"video_id": v["video_id"], "new_title": v["title"],
                             "thumbnail_idea": "One clear subject, three-word caption, bright background.",
                             "why": "Template mode: connect ANTHROPIC_API_KEY for real rewrites."}
                            for v in metrics.get("recent_uploads", [])]
        data["plan_intro"] = "A month of steady posting aimed at your goals."
        data["schedule"] = [{"day": d, "format": "long-form" if d % 7 == 1 else "short",
                             "idea": "Template mode idea"} for d in range(1, 31, 3)]
    return Narrative(data, "template", 0, 0, ["template writer (no ANTHROPIC_API_KEY)"])
