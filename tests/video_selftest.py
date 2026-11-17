"""Render one real Premium walkthrough (offline fake channel) with the real voice: python tests/video_selftest.py OUT.mp4
Used by the selftest GitHub Action to prove ffmpeg + Kokoro + slides (+ host clips) work on Linux."""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_e2e as T  # noqa: E402  (offline temp environment)
from creator_audit import db, video  # noqa: E402

db.init()
job = T.test_paid_flow("premium")
os.environ["VIDEO_TTS"] = "kokoro"
audit = video._audit_for(job["id"])
t = time.time()
out = video.render(dict(db.q1("SELECT * FROM jobs WHERE id=?", (job["id"],))), audit, Path(sys.argv[1]))
print(f"rendered {out} in {time.time() - t:.0f}s, {out.stat().st_size // 1024} KB, host clips: {video.host_clips_ready()}")
