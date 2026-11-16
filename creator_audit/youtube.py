"""YouTube Data API v3 client (public data only, API key auth).

Never used to collect contact details — the API gives none and the Developer Policies
forbid harvesting them. Set YOUTUBE_API_KEY=fake for a deterministic offline stand-in.
"""
from __future__ import annotations

import hashlib
import random
import re
from datetime import datetime, timedelta, timezone

import requests

from . import config

API = "https://www.googleapis.com/youtube/v3"


class YouTubeError(RuntimeError):
    pass


def _iso_duration(s: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0
    d, h, mi, se = (int(x) if x else 0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


class YouTube:
    def __init__(self, key: str | None = None):
        self.key = key if key is not None else config.YOUTUBE_API_KEY
        self.units_used = 0
        if not self.key:
            raise YouTubeError("YOUTUBE_API_KEY is not set")

    # ---- low level ----
    def _get(self, path: str, cost: int, **params) -> dict:
        params["key"] = self.key
        r = requests.get(f"{API}/{path}", params=params, timeout=20)
        self.units_used += cost
        if r.status_code != 200:
            raise YouTubeError(f"{path} {r.status_code}: {r.text[:300]}")
        return r.json()

    # ---- channel resolution ----
    def resolve_channel(self, url: str) -> str:
        url = url.strip()
        m = re.search(r"(UC[\w-]{22})", url)
        if m:
            return m.group(1)
        m = re.search(r"youtube\.com/@([\w.\-]+)", url) or re.fullmatch(r"@([\w.\-]+)", url)
        if m:
            data = self._get("channels", 1, part="id", forHandle="@" + m.group(1))
            if data.get("items"):
                return data["items"][0]["id"]
        m = re.search(r"youtube\.com/user/([\w.\-]+)", url)
        if m:
            data = self._get("channels", 1, part="id", forUsername=m.group(1))
            if data.get("items"):
                return data["items"][0]["id"]
        m = re.search(r"youtube\.com/c/([\w.\-]+)", url)
        if m:
            data = self._get("search", 100, part="snippet", type="channel", q=m.group(1), maxResults=1)
            if data.get("items"):
                return data["items"][0]["snippet"]["channelId"]
        raise YouTubeError(f"Couldn't find a YouTube channel for {url!r}")

    def channels(self, ids: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(ids), 50):
            data = self._get("channels", 1, part="snippet,statistics,contentDetails", id=",".join(ids[i:i + 50]))
            for it in data.get("items", []):
                st = it.get("statistics", {})
                out.append({
                    "id": it["id"],
                    "title": it["snippet"]["title"],
                    "subscribers": int(st.get("subscriberCount", 0)) if not st.get("hiddenSubscriberCount") else None,
                    "video_count": int(st.get("videoCount", 0)),
                    "uploads_playlist": it["contentDetails"]["relatedPlaylists"]["uploads"],
                })
        return out

    def upload_ids(self, playlist_id: str, limit: int | None = None) -> list[str]:
        limit = limit or config.MAX_VIDEOS_PULLED
        ids, page = [], None
        while len(ids) < limit:
            params = dict(part="contentDetails", playlistId=playlist_id, maxResults=50)
            if page:
                params["pageToken"] = page
            data = self._get("playlistItems", 1, **params)
            ids += [it["contentDetails"]["videoId"] for it in data.get("items", [])]
            page = data.get("nextPageToken")
            if not page:
                break
        return ids[:limit]

    def videos(self, ids: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(ids), 50):
            data = self._get("videos", 1, part="snippet,contentDetails,statistics", id=",".join(ids[i:i + 50]))
            for it in data.get("items", []):
                dur = _iso_duration(it["contentDetails"].get("duration", ""))
                out.append({
                    "video_id": it["id"],
                    "title": it["snippet"]["title"],
                    "published": it["snippet"]["publishedAt"],
                    "duration": dur,
                    "views_public": int(it.get("statistics", {}).get("viewCount", 0)),
                })
        return out

    def search_active_channels(self, query: str, days: int, n: int) -> list[str]:
        after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = self._get("search", 100, part="snippet", type="video", q=query, order="date",
                         publishedAfter=after, maxResults=min(n, 50), relevanceLanguage="en")
        return list(dict.fromkeys(it["snippet"]["channelId"] for it in data.get("items", [])))

    # ---- convenience ----
    def channel_with_videos(self, channel_id: str) -> tuple[dict, list[dict]]:
        chans = self.channels([channel_id])
        if not chans:
            raise YouTubeError(f"Channel {channel_id} not found")
        ch = chans[0]
        vids = self.videos(self.upload_ids(ch["uploads_playlist"]))
        return ch, vids


class FakeYouTube(YouTube):
    """Deterministic synthetic channels so the whole system runs offline."""

    def __init__(self):
        self.key = "fake"
        self.units_used = 0

    @staticmethod
    def _rng(seed: str) -> random.Random:
        return random.Random(int(hashlib.sha256(seed.encode()).hexdigest()[:12], 16))

    def resolve_channel(self, url: str) -> str:
        m = re.search(r"(UC[\w-]{22})", url)
        if m:
            return m.group(1)
        return "UC" + hashlib.sha256(url.encode()).hexdigest()[:22]

    def channels(self, ids: list[str]) -> list[dict]:
        self.units_used += 1
        out = []
        for cid in ids:
            r = self._rng(cid)
            out.append({"id": cid, "title": f"Demo Channel {cid[-5:]}",
                        "subscribers": r.randint(500, 150_000), "video_count": 120,
                        "uploads_playlist": "UU" + cid[2:]})
        return out

    def upload_ids(self, playlist_id: str, limit: int | None = None) -> list[str]:
        self.units_used += 1
        r = self._rng(playlist_id)
        n = r.randint(40, 90)
        return [f"v{hashlib.md5(f'{playlist_id}{i}'.encode()).hexdigest()[:10]}" for i in range(n)]

    def videos(self, ids: list[str]) -> list[dict]:
        self.units_used += 1
        words = ["easy", "workout", "recipe", "beginner", "mistakes", "tips", "challenge", "review",
                 "guide", "fast", "secret", "budget", "routine", "morning", "pro", "best"]
        out = []
        t = datetime.now(timezone.utc) - timedelta(days=2)
        base = None
        decline = None
        for i, vid in enumerate(ids):
            r = self._rng(vid)
            if base is None:
                rr = self._rng(ids[0])
                base = rr.randint(800, 20000)
                decline = rr.random() < 0.7
            age_factor = (0.45 if decline and i < 10 else 1.0)
            short = r.random() < 0.35
            views = int(base * age_factor * r.lognormvariate(0, 0.6) * (0.6 if short else 1))
            title = " ".join(r.sample(words, r.randint(3, 7))).title()
            if r.random() < 0.3:
                title = f"{r.randint(3, 10)} " + title
            out.append({"video_id": vid, "title": title, "published": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "duration": r.randint(20, 59) if short else r.randint(240, 1500),
                        "views_public": views})
            t -= timedelta(days=r.choice([2, 3, 4, 5, 7, 9, 14]) if i > 3 else r.choice([3, 6, 12]))
        return out

    def search_active_channels(self, query: str, days: int, n: int) -> list[str]:
        self.units_used += 100
        return ["UC" + hashlib.sha256(f"{query}{i}".encode()).hexdigest()[:22] for i in range(min(n, 12))]


def client() -> YouTube | None:
    if config.YOUTUBE_API_KEY.lower() == "fake":
        return FakeYouTube()
    if not config.YOUTUBE_API_KEY:
        return None
    return YouTube()
