"""Step 2: the 0-100 score and the ranked problem list — both computed in code.

Benchmarks below are starting values; tune them once outcome checks show which
thresholds predict growth (spec: "tuned once outcomes are tracked").
"""
from __future__ import annotations

import math

from . import config


def _clamp(x: float) -> float:
    return max(0.0, min(100.0, x))


def subscores(m: dict) -> dict:
    s: dict[str, float | None] = {}
    c = m["cadence"]
    upw = c["uploads_per_week"] or 0
    gap = c["longest_gap_days"] or 90
    s["consistency"] = _clamp(min(1, upw / config.TARGET_UPLOADS_PER_WEEK) * 70 + max(0, 30 - max(0, gap - 7) * 1.5))

    mo = m["momentum"]
    if mo["recent_median_views"] is not None and mo["previous_median_views"]:
        ratio = max(mo["recent_median_views"], 1) / mo["previous_median_views"]
        s["momentum"] = _clamp(50 + 50 * math.log2(ratio))
    else:
        s["momentum"] = None

    if "packaging" in m and m["packaging"]["avg_ctr_pct"] is not None:
        s["packaging"] = _clamp((m["packaging"]["avg_ctr_pct"] - 1.0) / 9.0 * 100)
    else:
        s["packaging"] = None

    if "retention" in m:
        r = m["retention"]
        fm = m["format_mix"]
        parts = []
        if r["longform_avg_pct_viewed"] is not None:
            parts.append((_clamp((r["longform_avg_pct_viewed"] - 10) / 45 * 100), fm["longform_count"]))
        if r["shorts_avg_pct_viewed"] is not None:
            parts.append((_clamp((r["shorts_avg_pct_viewed"] - 40) / 60 * 100), fm["shorts_count"]))
        tot = sum(w for _, w in parts)
        s["retention"] = sum(v * w for v, w in parts) / tot if tot else None
    else:
        s["retention"] = None

    if "conversion" in m:
        s["conversion"] = _clamp(m["conversion"]["subs_per_1k_views"] / 6.0 * 100)
    else:
        s["conversion"] = None

    return {k: (int(round(v)) if v is not None else None) for k, v in s.items()}


def overall(subs: dict) -> int:
    num = den = 0.0
    for k, v in subs.items():
        if v is None:
            continue
        w = config.WEIGHTS.get(k, 0)
        num += v * w
        den += w
    return int(round(num / den)) if den else 0


def problems(m: dict, subs: dict) -> list[dict]:
    """Rule-based problem candidates, most severe first. Each carries its evidence
    values (from metrics) and a fix_type tag used for advice grading."""
    out: list[dict] = []
    c, mo, o, fm = m["cadence"], m["momentum"], m["outliers"], m["format_mix"]

    def add(key, fix_type, severity, evidence):
        out.append({"key": key, "fix_type": fix_type, "severity": round(severity, 1), "evidence": evidence})

    if c["days_since_last_upload"] is not None and c["days_since_last_upload"] > 21:
        add("stalled", "cadence", 95, {"days_since_last_upload": c["days_since_last_upload"]})
    if c["uploads_per_week"] < config.TARGET_UPLOADS_PER_WEEK:
        add("low_cadence", "cadence", 100 - (subs["consistency"] or 0),
            {"uploads_per_week": c["uploads_per_week"], "uploads_last_90_days": c["uploads_last_90_days"]})
    if c["longest_gap_days"] > 14:
        add("long_gap", "cadence", min(90, c["longest_gap_days"] * 2), {"longest_gap_days": c["longest_gap_days"]})
    if mo["change_pct"] is not None and mo["change_pct"] < -15:
        add("falling_views", "content_direction", min(100, -mo["change_pct"]),
            {"recent_median_views": mo["recent_median_views"], "previous_median_views": mo["previous_median_views"],
             "change_pct": mo["change_pct"]})
    if "packaging" in m:
        p = m["packaging"]
        if p["avg_ctr_pct"] is not None and p["avg_ctr_pct"] < 4.0:
            add("low_ctr", "packaging", 100 - (subs["packaging"] or 0), {"avg_ctr_pct": p["avg_ctr_pct"]})
        if p["recent_count"] and p["recent_below_avg_count"] / p["recent_count"] >= 0.6:
            add("recent_ctr_below_avg", "packaging", 60 + p["recent_below_avg_count"] * 3,
                {"recent_below_avg_count": p["recent_below_avg_count"], "recent_count": p["recent_count"],
                 "avg_ctr_pct": p["avg_ctr_pct"]})
    if "retention" in m:
        r = m["retention"]
        if r["longform_avg_pct_viewed"] is not None and r["longform_avg_pct_viewed"] < 35:
            add("low_longform_retention", "retention", 100 - (subs["retention"] or 0) + 5,
                {"longform_avg_pct_viewed": r["longform_avg_pct_viewed"]})
        if r["shorts_avg_pct_viewed"] is not None and r["shorts_avg_pct_viewed"] < 70:
            add("low_shorts_retention", "retention", 70 - r["shorts_avg_pct_viewed"] / 2 + 30,
                {"shorts_avg_pct_viewed": r["shorts_avg_pct_viewed"]})
    if "conversion" in m and m["conversion"]["subs_per_1k_views"] < 2:
        add("low_conversion", "conversion", 100 - (subs["conversion"] or 0),
            {"subs_per_1k_views": m["conversion"]["subs_per_1k_views"]})
    sv, lv = fm["shorts_views_per_upload"], fm["longform_views_per_upload"]
    if sv and lv:
        if sv > 1.5 * lv and fm["shorts_share_pct"] < 40:
            add("format_mismatch_shorts", "format", 55, {"shorts_views_per_upload": sv, "longform_views_per_upload": lv,
                                                         "shorts_share_pct": fm["shorts_share_pct"]})
        elif lv > 1.5 * sv and fm["longform_share_pct"] < 40:
            add("format_mismatch_longform", "format", 55, {"shorts_views_per_upload": sv, "longform_views_per_upload": lv,
                                                           "longform_share_pct": fm["longform_share_pct"]})
    if o["count"] and o["traits"]:
        add("outliers_not_repeated", "content_direction", 35 + min(o["count"], 5),
            {"count": o["count"], "channel_median_views": o["channel_median_views"], **{
                k: v for k, v in o["traits"].items() if k in ("top_weekday", "common_title_words",
                                                             "outlier_median_minutes", "other_median_minutes")}})

    # pad with the weakest areas so paid tiers always get five
    have = {p["fix_type"] for p in out}
    for k, v in sorted(((k, v) for k, v in subs.items() if v is not None), key=lambda kv: kv[1]):
        if len(out) >= 5:
            break
        ft = {"consistency": "cadence", "momentum": "content_direction"}.get(k, k)
        if ft not in have:
            add(f"opportunity_{k}", ft, 30 - v / 10, {f"{k}_score": v})
            have.add(ft)
    out.sort(key=lambda p: -p["severity"])
    return out
