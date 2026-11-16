"""Step 1 of the audit engine: deterministic metrics with pandas.

Every number that can appear in a report is produced here (or in scoring.py) and
rounded here, so the number check can match Claude's text against exact values.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone

import pandas as pd

from . import config

STOPWORDS = set("""a an the and or but of to in on for with my your our is are was were be how what why when
i you we it this that these those at by from vs vs. me do does did not no so up out new video part ep episode
| - & + : ! ? / #""".split())
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def merge_sources(csv_df: pd.DataFrame | None, api_videos: list[dict] | None) -> pd.DataFrame:
    """Combine Studio CSV analytics with public API data (titles, dates, durations, views)."""
    api = pd.DataFrame(api_videos or [])
    if csv_df is None or csv_df.empty:
        if api.empty:
            raise ValueError("No video data available")
        df = api.copy()
        df["views"] = df["views_public"]
    elif api.empty:
        df = csv_df.copy()
        if "published" not in df or df["published"].isna().all():
            raise ValueError("The export has no publish dates and the YouTube API isn't configured.")
    else:
        # API is the master list (every video); CSV adds the analytics
        csv_cols = [c for c in csv_df.columns if c not in ("title", "published", "duration") or c not in api.columns]
        df = api.merge(csv_df[csv_cols], on="video_id", how="left", suffixes=("", "_csv"))
        if "views" in df:
            df["views"] = df["views"].fillna(df["views_public"])
        else:
            df["views"] = df["views_public"]
    df["published"] = pd.to_datetime(df["published"], utc=True, errors="coerce")
    df = df[df["published"].notna()].copy()
    df["views"] = pd.to_numeric(df["views"], errors="coerce").fillna(0)
    if "duration" not in df:
        df["duration"] = pd.NA
    df["duration"] = pd.to_numeric(df["duration"], errors="coerce")
    df["is_short"] = df["duration"].le(config.SHORTS_MAX_SECONDS) & df["duration"].gt(0)
    df["title"] = df.get("title", pd.Series([""] * len(df))).fillna("").astype(str)
    return df.sort_values("published", ascending=False).reset_index(drop=True)


def _r(x, nd=1):
    if x is None or pd.isna(x):
        return None
    return round(float(x), nd) if nd else int(round(float(x)))


def _words(title: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9']+", title.lower()) if w not in STOPWORDS and len(w) > 2 and not w.isdigit()]


def compute(df: pd.DataFrame, channel: dict, as_of: datetime | None = None) -> dict:
    as_of = as_of or datetime.now(timezone.utc)
    m: dict = {
        "channel": {
            "id": channel.get("id"),
            "title": channel.get("title"),
            "subscribers": channel.get("subscribers"),
            "videos_analyzed": int(len(df)),
            "has_studio_data": bool("ctr" in df and df["ctr"].notna().any()),
        }
    }

    # ---- cadence ----
    cutoff = as_of - pd.Timedelta(days=90)
    recent = df[df["published"] >= cutoff].sort_values("published")
    dates = list(recent["published"]) + [pd.Timestamp(as_of)]
    if len(recent):
        prev = recent["published"].iloc[0]
        gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
        gaps.append((recent["published"].iloc[0] - cutoff).days if len(recent) else 90)
        longest_gap = max(gaps) if gaps else 90
    else:
        longest_gap = 90
    days_since = (pd.Timestamp(as_of) - df["published"].max()).days if len(df) else None
    m["cadence"] = {
        "uploads_last_90_days": int(len(recent)),
        "uploads_per_week": _r(len(recent) / (90 / 7), 1),
        "longest_gap_days": int(longest_gap),
        "days_since_last_upload": int(days_since) if days_since is not None else None,
    }

    # ---- momentum ----
    last10 = df.head(10)
    prev10 = df.iloc[10:20]
    rmed = last10["views"].median() if len(last10) else None
    pmed = prev10["views"].median() if len(prev10) else None
    change = ((rmed - pmed) / pmed * 100) if (rmed is not None and pmed) else None
    m["momentum"] = {
        "recent_median_views": _r(rmed, 0),
        "previous_median_views": _r(pmed, 0),
        "change_pct": _r(change, 1),
        "recent_count": int(len(last10)),
        "previous_count": int(len(prev10)),
    }

    # ---- outliers ----
    med = df["views"].median()
    thr = 2 * med
    out = df[df["views"] > thr]
    rest = df[df["views"] <= thr]
    traits: dict = {}
    if len(out):
        ow = Counter(w for t in out["title"] for w in set(_words(t)))
        rw = Counter(w for t in rest["title"] for w in set(_words(t)))
        common = [w for w, c in ow.most_common(20)
                  if c >= 2 and (c / len(out)) > 1.5 * (rw.get(w, 0) / max(len(rest), 1))][:5]
        wd = out["published"].dt.dayofweek.value_counts()
        traits = {
            "outlier_median_minutes": _r(out["duration"].median() / 60, 1) if out["duration"].notna().any() else None,
            "other_median_minutes": _r(rest["duration"].median() / 60, 1) if rest["duration"].notna().any() else None,
            "outlier_median_title_words": _r(out["title"].str.split().str.len().median(), 0),
            "other_median_title_words": _r(rest["title"].str.split().str.len().median(), 0),
            "outlier_shorts_share_pct": _r(out["is_short"].mean() * 100, 1),
            "common_title_words": common,
            "top_weekday": WEEKDAYS[int(wd.index[0])],
            "top_weekday_share_pct": _r(wd.iloc[0] / len(out) * 100, 1),
        }
    m["outliers"] = {
        "channel_median_views": _r(med, 0),
        "threshold_views": _r(thr, 0),
        "count": int(len(out)),
        "share_pct": _r(len(out) / max(len(df), 1) * 100, 1),
        "top": [{"title": r.title, "views": _r(r.views, 0), "multiple": _r(r.views / med if med else 0, 1),
                 "short": bool(r.is_short)}
                for r in out.sort_values("views", ascending=False).head(5).itertuples()],
        "traits": traits,
    }

    # ---- packaging (needs Studio data) ----
    if m["channel"]["has_studio_data"]:
        d = df[df["ctr"].notna()]
        if "impressions" in d and d["impressions"].fillna(0).sum() > 0:
            avg_ctr = (d["ctr"] * d["impressions"]).sum() / d["impressions"].sum()
        else:
            avg_ctr = d["ctr"].mean()
        r10 = d.head(10)
        below = r10[r10["ctr"] < avg_ctr]
        eligible = d[d.get("impressions", pd.Series(0, index=d.index)).fillna(0) >= 100] if "impressions" in d else d
        m["packaging"] = {
            "avg_ctr_pct": _r(avg_ctr, 1),
            "recent_below_avg_count": int(len(below)),
            "recent_count": int(len(r10)),
            "lowest": [{"title": r.title, "ctr_pct": _r(r.ctr, 1)} for r in eligible.nsmallest(3, "ctr").itertuples()],
            "highest": [{"title": r.title, "ctr_pct": _r(r.ctr, 1)} for r in eligible.nlargest(3, "ctr").itertuples()],
        }
        lf = d[~d["is_short"]]["avg_pct_viewed"].dropna()
        sh = d[d["is_short"]]["avg_pct_viewed"].dropna()
        m["retention"] = {
            "longform_avg_pct_viewed": _r(lf.mean(), 1) if len(lf) else None,
            "shorts_avg_pct_viewed": _r(sh.mean(), 1) if len(sh) else None,
        }
        v = d["views"].sum()
        s = d["subs_gained"].fillna(0).sum()
        m["conversion"] = {
            "subs_per_1k_views": _r(s / v * 1000 if v else 0, 2),
            "total_subs_gained": _r(s, 0),
            "total_views": _r(v, 0),
        }

    # ---- format mix ----
    sh = df[df["is_short"]]
    lf = df[~df["is_short"]]
    m["format_mix"] = {
        "shorts_count": int(len(sh)),
        "longform_count": int(len(lf)),
        "shorts_share_pct": _r(len(sh) / max(len(df), 1) * 100, 1),
        "longform_share_pct": _r(len(lf) / max(len(df), 1) * 100, 1),
        "shorts_views_per_upload": _r(sh["views"].mean(), 0) if len(sh) else None,
        "longform_views_per_upload": _r(lf["views"].mean(), 0) if len(lf) else None,
        "views_per_upload": _r(df["views"].mean(), 0),
    }

    # recent uploads for Premium title rewrites (titles only; no numbers needed)
    m["recent_uploads"] = [{"video_id": r.video_id, "title": r.title, "views": _r(r.views, 0),
                            "ctr_pct": _r(getattr(r, "ctr", None), 1) if m["channel"]["has_studio_data"] else None}
                           for r in df.head(5).itertuples()]
    return m
