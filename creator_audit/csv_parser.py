"""Validate and normalise a YouTube Studio Advanced-mode export.

Studio exports a ZIP with "Table data.csv", "Chart data.csv" and "Totals.csv". Creators
may upload the ZIP or just the table CSV. Column names depend on which metrics they
turned on, so we map known aliases and report exactly which ones are missing.
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field

import pandas as pd


class UploadError(ValueError):
    """Raised with a creator-facing fix-it message."""


# canonical name -> accepted header spellings (lower-cased, punctuation-insensitive)
ALIASES: dict[str, list[str]] = {
    "video_id": ["content", "video", "video id"],
    "title": ["video title", "title"],
    "published": ["video publish time", "publish time", "published", "upload date"],
    "duration": ["duration", "duration (seconds)"],
    "views": ["views"],
    "watch_hours": ["watch time (hours)", "watch time hours"],
    "impressions": ["impressions"],
    "ctr": ["impressions click-through rate (%)", "impressions click through rate (%)",
            "impressions click-through rate", "click-through rate (%)", "ctr"],
    "avg_view_duration": ["average view duration", "avg view duration"],
    "avg_pct_viewed": ["average percentage viewed (%)", "average percentage viewed"],
    "subs_gained": ["subscribers gained", "subscribers"],
}

REQUIRED = ["video_id", "views", "impressions", "ctr", "avg_pct_viewed", "subs_gained"]

# What the upload guide tells creators to switch on in Studio
REQUIRED_HUMAN = {
    "video_id": "Content (the default first column — export from the Content tab)",
    "views": "Views",
    "impressions": "Impressions",
    "ctr": "Impressions click-through rate (%)",
    "avg_pct_viewed": "Average percentage viewed (%)",
    "subs_gained": "Subscribers gained (or Subscribers)",
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower().replace("﻿", ""))


def _hms_to_seconds(v) -> float | None:
    if pd.isna(v):
        return None
    s = str(v).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    parts = s.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    sec = 0.0
    for n in nums:
        sec = sec * 60 + n
    return sec


@dataclass
class ParsedUpload:
    df: pd.DataFrame
    warnings: list[str] = field(default_factory=list)


def _read_bytes(filename: str, raw: bytes) -> pd.DataFrame:
    name = (filename or "").lower()
    if name.endswith(".zip") or raw[:2] == b"PK":
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile as e:
            raise UploadError("That ZIP file couldn't be opened. Re-download the export from YouTube Studio and try again.") from e
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        table = [n for n in names if "table data" in n.lower()]
        pick = table[0] if table else (names[0] if names else None)
        if not pick:
            raise UploadError("The ZIP has no CSV inside. Upload the ZIP that YouTube Studio's Export button gives you.")
        raw = zf.read(pick)
    elif not name.endswith(".csv"):
        raise UploadError("Please upload the .csv (or the .zip) from YouTube Studio's export — other file types can't be read.")
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return pd.read_csv(io.BytesIO(raw), encoding=enc)
        except (UnicodeDecodeError, pd.errors.ParserError):
            continue
        except pd.errors.EmptyDataError as e:
            raise UploadError("That file is empty. Export again from YouTube Studio.") from e
    raise UploadError("We couldn't read that CSV. Make sure it's the unedited export from YouTube Studio.")


def parse(filename: str, raw: bytes) -> ParsedUpload:
    if not raw:
        raise UploadError("No file received. Choose your YouTube Studio export and try again.")
    df = _read_bytes(filename, raw)
    lookup = {_norm(c): c for c in df.columns}

    rename: dict[str, str] = {}
    for canon, aliases in ALIASES.items():
        for a in aliases:
            if a in lookup and lookup[a] not in rename:
                rename[lookup[a]] = canon
                break
    # "Subscribers" may be net subscribers; prefer an explicit "Subscribers gained"
    if "subscribers gained" in lookup:
        rename = {k: v for k, v in rename.items() if v != "subs_gained"}
        rename[lookup["subscribers gained"]] = "subs_gained"

    missing = [c for c in REQUIRED if c not in rename.values()]
    if missing:
        need = "; ".join(REQUIRED_HUMAN[m] for m in missing)
        if "video_id" in missing and len(missing) >= 4:
            raise UploadError(
                "This doesn't look like a YouTube Studio Content export. In Studio go to "
                "Analytics → Advanced mode → Content tab, add the metrics listed in step 2, then Export → CSV."
            )
        raise UploadError(
            f"Your export is missing these columns: {need}. In Advanced mode click the metric "
            "picker, add them, then export again."
        )

    df = df.rename(columns=rename)[list(dict.fromkeys(rename.values()))]
    df = df[df["video_id"].astype(str).str.strip().str.lower() != "total"]
    df = df[df["video_id"].notna()]
    df["video_id"] = df["video_id"].astype(str).str.strip()

    for col in ["views", "watch_hours", "impressions", "ctr", "avg_pct_viewed", "subs_gained"]:
        if col in df:
            df[col] = pd.to_numeric(df[col].astype(str).str.replace(",", "").str.replace("%", ""), errors="coerce")
    if "duration" in df:
        df["duration"] = df["duration"].map(_hms_to_seconds)
    if "avg_view_duration" in df:
        df["avg_view_duration"] = df["avg_view_duration"].map(_hms_to_seconds)
    if "published" in df:
        df["published"] = pd.to_datetime(df["published"], errors="coerce", utc=True, format="mixed")

    warnings = []
    if len(df) < 5:
        raise UploadError(
            f"Only {len(df)} videos were in the export. Set the date range to 'Lifetime' (or at least the last 365 days) and export again."
        )
    if len(df) < 20:
        warnings.append("Fewer than 20 videos — momentum and outlier findings will be less reliable.")
    if df["views"].fillna(0).sum() == 0:
        raise UploadError("Every video shows 0 views. Check the date range in Studio is 'Lifetime' and export again.")
    return ParsedUpload(df=df.reset_index(drop=True), warnings=warnings)
