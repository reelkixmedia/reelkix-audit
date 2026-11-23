"""Premium mascot video walkthrough: the Reelkix host talks the creator through their audit and both plans.

Free and automatic: Reelkix-style slides (Pillow) + the host's talk/point/present clips (rendered once in
Blender, bundled in video_assets/, VIDEO_MASCOT) + the free Kokoro voice (VIDEO_VOICE) + ffmpeg. About 1.5-2 minutes, 720p,
a few MB, stored in the database. Run by the GitHub Action (python -m creator_audit cron) or on any PC with
`python -m creator_audit videos`. Promise to the customer: within 2 days (VIDEO_PROMISE_DAYS); an admin alert
goes out if a video is still missing after VIDEO_ALERT_HOURS.

VIDEO_TTS=silent makes silent audio of the right length (tests, or a machine without the voice model).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import traceback
import wave
from datetime import timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import config, db, emailer, scoring

log = logging.getLogger("video")
ASSETS = Path(__file__).resolve().parent / "video_assets"
W, H, FPS = 1280, 720, 24
PANEL = (836, 64, 420, 560)  # x, y, w, h of the mascot panel (3:4, like the clips)
BG, PANEL_BG, LINE = (7, 11, 22), (13, 20, 36), (34, 48, 77)
WHITE, TEXT2, MUTED, AMBER = (255, 255, 255), (183, 195, 220), (154, 168, 196), (255, 181, 71)
ORD = ["one", "two", "three", "four", "five"]
SUBS = ["consistency", "momentum", "packaging", "retention", "conversion"]


# ------------------------------------------------------------------ fonts + text
def _font(name: str, size: int, weight: str = "Bold") -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(ASSETS / "fonts" / name), size)
    try:
        f.set_variation_by_name(weight)
    except Exception:  # noqa: BLE001  (static font or unknown style)
        pass
    return f


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= width:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _text(draw, xy, text, font, fill, width, spacing=1.25, max_lines=None) -> int:
    x, y = xy
    lines = _wrap(draw, text, font, width)
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(".,;:") + "…"
    lh = int(font.size * spacing)
    for ln in lines:
        draw.text((x, y), ln, font=font, fill=fill)
        y += lh
    return y


def _base(kicker: str) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    for gx in range(0, W, 64):  # faint floor grid, like reelkix.com
        d.line([(gx, H - 150), (gx, H)], fill=(12, 20, 34))
    for gy in range(H - 150, H, 32):
        d.line([(0, gy), (W, gy)], fill=(12, 20, 34))
    d.text((56, 52), "REELKIX", font=_font("Unbounded.ttf", 26, "ExtraBold"), fill=WHITE)
    d.text((56, 92), kicker.upper(), font=_font("Manrope.ttf", 17, "Bold"), fill=AMBER)
    x, y, w, h = PANEL
    d.rounded_rectangle([x - 6, y - 6, x + w + 6, y + h + 6], radius=18, fill=PANEL_BG, outline=LINE, width=2)
    return im, d


# ------------------------------------------------------------------ slides
def slide_intro(channel: str, score: int, subs: dict) -> Image.Image:
    im, d = _base("Your channel audit")
    y = _text(d, (56, 150), channel, _font("Unbounded.ttf", 40), WHITE, 720, max_lines=2)
    cx, cy, r = 170, y + 130, 105
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=LINE, width=18)
    d.arc([cx - r, cy - r, cx + r, cy + r], start=-90, end=-90 + 360 * max(0, min(score, 100)) / 100, fill=AMBER, width=18)
    s = str(score)
    f = _font("Unbounded.ttf", 58)
    d.text((cx - d.textlength(s, font=f) / 2, cy - 44), s, font=f, fill=WHITE)
    f2 = _font("Manrope.ttf", 18, "Bold")
    d.text((cx - d.textlength("out of 100", font=f2) / 2, cy + 22), "out of 100", font=f2, fill=MUTED)
    bx, by = 330, y + 40
    for k in SUBS:
        v = subs.get(k)
        d.text((bx, by), k.capitalize(), font=_font("Manrope.ttf", 19, "Bold"), fill=TEXT2)
        d.rounded_rectangle([bx + 160, by + 8, bx + 460, by + 18], radius=5, fill=LINE)
        if v is not None:
            d.rounded_rectangle([bx + 160, by + 8, bx + 160 + int(300 * v / 100), by + 18], radius=5, fill=AMBER)
        d.text((bx + 474, by - 2), "n/a" if v is None else str(int(v)), font=_font("Manrope.ttf", 19, "ExtraBold"), fill=WHITE)
        by += 44
    return im


def slide_problem(i: int, n: int, p: dict) -> Image.Image:
    im, d = _base(f"Problem {i} of {n}")
    y = _text(d, (56, 150), p["title"], _font("Unbounded.ttf", 36), WHITE, 730, max_lines=3)
    y = _text(d, (56, y + 18), p["evidence"], _font("Manrope.ttf", 22, "Medium"), TEXT2, 730, max_lines=4)
    y += 22
    fix_lines = _wrap(d, p["fix"], _font("Manrope.ttf", 21, "Medium"), 680)[:6]
    box_h = 56 + len(fix_lines) * 27
    d.rounded_rectangle([56, y, 786, y + box_h], radius=10, fill=PANEL_BG, outline=(90, 66, 32), width=2)
    d.text((78, y + 14), "THE FIX", font=_font("Manrope.ttf", 16, "ExtraBold"), fill=AMBER)
    _text(d, (78, y + 42), " ".join(fix_lines), _font("Manrope.ttf", 21, "Medium"), WHITE, 680, max_lines=6)
    return im


def slide_plan(kicker: str, title: str, items: list[str]) -> Image.Image:
    im, d = _base(kicker)
    y = _text(d, (56, 150), title, _font("Unbounded.ttf", 34), WHITE, 730, max_lines=2) + 18
    for it in items:
        d.ellipse([60, y + 9, 74, y + 23], fill=AMBER)
        y = _text(d, (92, y), it, _font("Manrope.ttf", 23, "SemiBold"), WHITE, 690, max_lines=2) + 14
    return im


def slide_outro() -> Image.Image:
    im, d = _base("That's the walkthrough")
    y = _text(d, (56, 160), "Your full report and both plans are in your inbox.", _font("Unbounded.ttf", 36), WHITE, 730)
    _text(d, (56, y + 24), "Follow the plan day by day. At the end of the sixty days we'll send you a link to run a fresh "
                           "audit and see your new score next to this one.", _font("Manrope.ttf", 23, "Medium"), TEXT2, 720)
    d.text((56, H - 110), "reelkix.com", font=_font("Unbounded.ttf", 26), fill=AMBER)
    return im


# ------------------------------------------------------------------ script
def _theme(s: str) -> str:
    return s.split(":", 1)[-1].strip() if ":" in s else s


def segments(job: dict, audit: dict) -> list[dict]:
    m, nar, score, subs = audit["metrics"], audit["narrative"], audit["score"], audit["subscores"]
    channel = m["channel"].get("title") or job.get("channel_title") or "your channel"
    known = {k: v for k, v in subs.items() if v is not None}
    best = max(known, key=known.get) if known else None
    worst = min(known, key=known.get) if known else None
    me = f"Hi, I'm {config.VIDEO_HOST_NAME} from Reelkix." if config.VIDEO_HOST_NAME else "Hi, it's Reelkix."
    intro = f"{me} I went through the numbers for {channel}, and your score is {score} out of 100."
    if best and worst and best != worst:
        intro += f" Your strongest area is {best}, and the one holding you back most is {worst}."
    intro += " Let's walk through what to fix, and your plan for the next sixty days."
    segs = [{"text": intro, "slide": slide_intro(channel, score, subs), "gesture": "present"}]
    probs = nar.get("problems", [])[:3]
    for i, p in enumerate(probs, 1):
        segs.append({"text": f"Problem {ORD[i - 1]}: {p['title']}. {p['evidence']} Here's the fix. {p['fix']}",
                     "slide": slide_problem(i, len(probs), p), "gesture": "point"})
    w30 = []
    for r in nar.get("schedule", []):
        if r.get("theme") and r["theme"] not in w30 and r["theme"].startswith("Week"):
            w30.append(r["theme"])
    if w30:
        segs.append({"text": "Now your first thirty days. " + nar.get("plan_intro", "") + " "
                             + " ".join(f"{t}." for t in w30) + " Then on day thirty, a quick check-in.",
                     "slide": slide_plan("Your 30-day plan: fix", "Days 1 to 30", [_theme(t) for t in w30] + ["Day 30: check-in"]),
                     "gesture": "present"})
    w60 = []
    for r in nar.get("schedule60", []):
        if r.get("theme") and r["theme"] not in w60 and r["theme"].startswith("Week"):
            w60.append(r["theme"])
    if w60:
        segs.append({"text": "Then days thirty-one to sixty. " + nar.get("plan60_intro", "") + " "
                             + " ".join(f"{t}." for t in w60),
                     "slide": slide_plan("Your 60-day plan: grow", "Days 31 to 60", [_theme(t) for t in w60] + ["Day 60: re-audit"]),
                     "gesture": "point"})
    segs.append({"text": "That's it. Every detail, and both plans day by day, are in your report. "
                         "Pin it, follow it, and I'll see you at the re-audit. Let's get to work!",
                 "slide": slide_outro(), "gesture": "present"})
    return segs


# ------------------------------------------------------------------ voice
_kokoro = None


def _speak(text: str, out: Path) -> float:
    """Write a mono 16-bit WAV; returns seconds."""
    import numpy as np
    if os.environ.get("VIDEO_TTS", "kokoro") == "silent":
        secs = max(2.0, len(text.split()) / 2.7)
        _wav(out, np.zeros(int(24000 * secs), dtype=np.float32), 24000)
        return secs
    global _kokoro
    if _kokoro is None:
        from kokoro_onnx import Kokoro
        mdir = Path(os.environ.get("KOKORO_DIR", Path.home() / ".cache" / "kokoro"))
        _kokoro = Kokoro(str(mdir / "kokoro-v1.0.int8.onnx"), str(mdir / "voices-v1.0.bin"))
    audio, sr = _kokoro.create(text, voice=config.VIDEO_VOICE, speed=config.VIDEO_SPEED, lang="en-us")
    _wav(out, audio, sr)
    return len(audio) / sr


def _wav(path: Path, audio, sr: int) -> None:
    import numpy as np
    pcm = (np.clip(np.asarray(audio, dtype=np.float32), -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


# ------------------------------------------------------------------ render
def _ffmpeg() -> str:
    exe = os.environ.get("FFMPEG") or shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg not found")
    return exe


def _run(args: list[str]) -> None:
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {r.stderr[-1500:]}")


GESTURES = ("talk", "point", "present")


def host_clips_ready() -> bool:
    """True once the host's clips are in video_assets/ (<VIDEO_MASCOT>_talk/point/present.mp4, 3:4)."""
    return all((ASSETS / f"{config.VIDEO_MASCOT}_{g}.mp4").exists() for g in GESTURES)


def _brand_panel(im: Image.Image) -> Image.Image:
    """Until the host's clips exist, the mascot panel shows the Reelkix mark instead."""
    d = ImageDraw.Draw(im)
    x, y, w, h = PANEL
    f1, f2 = _font("Unbounded.ttf", 44, "ExtraBold"), _font("Manrope.ttf", 18, "Bold")
    for txt, f, fill, dy in (("REELKIX", f1, WHITE, -36), ("CHANNEL AUDIT", f2, AMBER, 28)):
        d.text((x + (w - d.textlength(txt, font=f)) / 2, y + h / 2 + dy), txt, font=f, fill=fill)
    return im


def render(job: dict, audit: dict, out_path: Path) -> Path:
    ff = _ffmpeg()
    mascot = config.VIDEO_MASCOT
    host = host_clips_ready()
    talk, tmp = ASSETS / f"{mascot}_talk.mp4", Path(tempfile.mkdtemp(prefix="walk_"))
    try:
        loop = tmp / "talk_loop.mp4"
        if host:  # talk clip forward + reversed = a loop with no jump
            _run([ff, "-v", "error", "-y", "-i", str(talk), "-filter_complex",
                  "[0:v]split[a][b];[b]reverse[r];[a][r]concat=n=2:v=1[v]", "-map", "[v]", "-an",
                  "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(loop)])
        else:
            log.warning("no host clips for '%s' in %s: rendering slides + voice only", mascot, ASSETS)
        x, y, w, h = PANEL
        parts = []
        for i, s in enumerate(segments(job, audit)):
            png, wav, mp4 = tmp / f"s{i}.png", tmp / f"s{i}.wav", tmp / f"s{i}.mp4"
            (s["slide"] if host else _brand_panel(s["slide"])).save(png)
            secs = _speak(s["text"], wav) + 0.6
            if not host:
                _run([ff, "-v", "error", "-y", "-loop", "1", "-framerate", str(FPS), "-i", str(png), "-i", str(wav),
                      "-filter_complex", f"[0:v]trim=duration={secs:.2f},format=yuv420p[v];[1:a]apad,atrim=duration={secs:.2f}[a]",
                      "-map", "[v]", "-map", "[a]", "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
                      "-tune", "stillimage", "-c:a", "aac", "-b:a", "80k", "-ar", "24000", "-ac", "1", str(mp4)])
                parts.append(mp4)
                continue
            gesture = ASSETS / f"{mascot}_{s['gesture']}.mp4"
            fc = (f"[2:v]scale={w}:{h},setsar=1,fps={FPS}[g];[1:v]scale={w}:{h},setsar=1,fps={FPS}[t];"
                  f"[g][t]concat=n=2:v=1,trim=duration={secs:.2f},setpts=PTS-STARTPTS[m];"
                  f"[0:v][m]overlay={x}:{y}:shortest=0,trim=duration={secs:.2f},format=yuv420p[v];"
                  f"[3:a]apad,atrim=duration={secs:.2f}[a]")
            _run([ff, "-v", "error", "-y", "-loop", "1", "-framerate", str(FPS), "-i", str(png),
                  "-stream_loop", "-1", "-i", str(loop), "-i", str(gesture), "-i", str(wav),
                  "-filter_complex", fc, "-map", "[v]", "-map", "[a]", "-r", str(FPS),
                  "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-tune", "animation",
                  "-c:a", "aac", "-b:a", "80k", "-ar", "24000", "-ac", "1", str(mp4)])
            parts.append(mp4)
        lst = tmp / "list.txt"
        lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _run([ff, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
              "-movflags", "+faststart", str(out_path)])
        return out_path
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ queue
def _audit_for(job_id: int) -> dict | None:
    a = db.q1("SELECT * FROM audit_log WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,))
    if not a or not a["metrics_json"]:
        return None
    m, subs = json.loads(a["metrics_json"]), json.loads(a["subscores_json"])
    return {"metrics": m, "subscores": subs, "score": int(a["score"]), "narrative": json.loads(a["narrative_json"]),
            "problems": scoring.problems(m, subs)}


def due_date(video_job) -> str:
    created = db.parse_iso(video_job["created_at"]) or db.now()
    return (created + timedelta(days=config.VIDEO_PROMISE_DAYS)).astimezone().strftime("%A, %B %-d" if os.name != "nt" else "%A, %B %#d")


def _claim():
    stale = db.iso(db.now() - timedelta(hours=1))  # a worker that died mid-render releases its job
    db.ex("UPDATE video_jobs SET status='pending' WHERE status='claimed' AND claimed_at<?", (stale,))
    return db.q1("UPDATE video_jobs SET status='claimed', claimed_at=? WHERE id=(SELECT id FROM video_jobs "
                 "WHERE status='pending' AND kind='walkthrough' ORDER BY id LIMIT 1) AND status='pending' RETURNING *",
                 (db.iso(),))


def process_one() -> bool:
    vj = _claim()
    if not vj:
        return False
    job = db.q1("SELECT * FROM jobs WHERE id=?", (vj["job_id"],))
    audit = _audit_for(vj["job_id"]) if job else None
    if not job or not audit:
        db.update("video_jobs", vj["id"], {"status": "failed", "error": "audit data gone (deleted?)"})
        return True
    try:
        with tempfile.TemporaryDirectory() as td:
            out = render(dict(job), audit, Path(td) / "walkthrough.mp4")
            ref = db.put_blob(f"video/{vj['id']}.mp4", out.read_bytes())
        db.update("video_jobs", vj["id"], {"status": "done", "output_path": ref, "error": None})
        _notify(dict(job))
        log.info("video %s done", vj["id"])
    except Exception as e:  # noqa: BLE001
        tries = (vj["attempts"] or 0) + 1
        err = f"{e}\n{traceback.format_exc()}"[-2000:]
        db.update("video_jobs", vj["id"], {"status": "pending" if tries < 3 else "failed", "attempts": tries, "error": err})
        log.exception("video %s failed (try %s)", vj["id"], tries)
        if tries >= 3:
            emailer.alert(f"Video walkthrough {vj['id']} failed", err)
    return True


def _notify(job: dict) -> None:
    if not job.get("email"):
        return
    link = config.link(f"/r/{job['report_token']}")
    title = job.get("channel_title") or "your channel"
    try:
        emailer.send("delivery", job["email"], f"Your video walkthrough is ready: {title}",
                     f'<p>Your video walkthrough of the audit for <b>{title}</b>, including both plans, is ready.</p>'
                     f'<p><a href="{link}">Watch it on your report page</a> (you can download it there too).</p>',
                     f"Your video walkthrough for {title} is ready: {link}", job_id=job["id"], order_id=job.get("order_id"))
    except Exception as e:  # noqa: BLE001
        log.error("video ready email failed: %s", type(e).__name__)  # no message: it can contain the address (public logs)


def process_due(limit: int = 5) -> int:
    n = 0
    while n < limit and process_one():
        n += 1
    return n


def check_deadlines() -> int:
    """Alert the admin once if a video is still missing VIDEO_ALERT_HOURS after the order (promise: 2 days)."""
    cutoff = db.iso(db.now() - timedelta(hours=config.VIDEO_ALERT_HOURS))
    late = db.q("SELECT * FROM video_jobs WHERE kind='walkthrough' AND status!='done' AND alerted_at IS NULL "
                "AND created_at<?", (cutoff,))
    for v in late:
        emailer.alert(f"Video walkthrough {v['id']} is late",
                      f"Video job {v['id']} (audit job {v['job_id']}) is still '{v['status']}' after "
                      f"{config.VIDEO_ALERT_HOURS} hours. The customer was promised it within "
                      f"{config.VIDEO_PROMISE_DAYS} days.\n\nLast error:\n{v['error'] or '-'}")
        db.update("video_jobs", v["id"], {"alerted_at": db.iso()})
    return len(late)
