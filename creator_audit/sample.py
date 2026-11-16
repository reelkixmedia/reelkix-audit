"""Generate a realistic fake YouTube Studio Content export for testing."""
from __future__ import annotations

import csv
import random
from datetime import datetime, timedelta, timezone

from .youtube import FakeYouTube


def write_sample(path: str, channel_url: str = "https://www.youtube.com/@democreator") -> str:
    """Matches the FakeYouTube channel for `channel_url`, so CSV + API data line up."""
    yt = FakeYouTube()
    cid = yt.resolve_channel(channel_url)
    vids = yt.videos(yt.upload_ids("UU" + cid[2:]))
    r = random.Random(7)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Content", "Video title", "Video publish time", "Duration", "Views", "Watch time (hours)",
                    "Subscribers gained", "Impressions", "Impressions click-through rate (%)",
                    "Average view duration", "Average percentage viewed (%)"])
        w.writerow(["Total", "", "", "", sum(v["views_public"] for v in vids), "", "", "", "", "", ""])
        for i, v in enumerate(vids):
            short = v["duration"] <= 60
            ctr = round(max(0.8, r.gauss(4.2 if i >= 10 else 3.1, 1.1)), 1)
            impressions = int(v["views_public"] / (ctr / 100) * 0.55)
            apv = round(min(140, max(8, r.gauss(78 if short else 31, 9))), 1)
            avd = int(v["duration"] * apv / 100)
            pub = datetime.fromisoformat(v["published"].replace("Z", "+00:00"))
            w.writerow([v["video_id"], v["title"], pub.strftime("%b %d, %Y"), v["duration"], v["views_public"],
                        round(v["views_public"] * avd / 3600, 1), int(v["views_public"] * r.uniform(0.0008, 0.004)),
                        impressions, ctr, f"0:{avd // 60:02d}:{avd % 60:02d}", apv])
    return path
