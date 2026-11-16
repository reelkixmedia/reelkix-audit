"""End-to-end smoke test, fully offline: fake YouTube, dev checkout, template writer, outbox email.

    python -m pytest tests -q        (or: python tests/test_e2e.py)
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="audit_test_")
os.environ.update({"DATA_DIR": TMP, "PREMIUM_ENABLED": "true", "YOUTUBE_API_KEY": "fake", "ANTHROPIC_API_KEY": "", "STRIPE_SECRET_KEY": "",
                   "SMTP_HOST": "", "ADMIN_PASSWORD": "pw", "ADMIN_EMAIL": "owner@example.com",
                   "BASE_URL": "http://testserver", "VIDEO_WORKER_KEY": "vk"})
# Same tests against Postgres: set TEST_DATABASE_URL to an empty database (tests/run_pg.py does this).
if os.environ.get("TEST_DATABASE_URL"):
    os.environ.update({"DATABASE_URL": os.environ["TEST_DATABASE_URL"], "ALLOW_DEV_CHECKOUT": "true", "ALLOW_OUTBOX": "true"})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from creator_audit import config, db, outreach, pipeline, tasks  # noqa: E402
from creator_audit.app import app  # noqa: E402
from creator_audit.sample import write_sample  # noqa: E402

AUTH = ("admin", "pw")


def drain():
    while pipeline.process_one():
        pass


def outbox():
    return sorted(config.OUTBOX_DIR.glob("*.eml"))


def test_csv_rejections():
    from creator_audit import csv_parser
    for raw, needle in [(b"a,b\n1,2\n", "doesn't look like"), (b"", "No file"),
                        (b"Content,Views,Impressions\nabc,1,2\n", "missing these columns")]:
        try:
            csv_parser.parse("x.csv", raw)
            raise AssertionError("should fail")
        except csv_parser.UploadError as e:
            assert needle in str(e), str(e)


def test_paid_flow(tier="standard"):
    c = TestClient(app)
    db.init()
    r = c.post("/checkout", data={"tier": tier, "email": "creator@example.com"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/upload/"), r.text
    up = r.headers["location"]
    assert c.get(up).status_code == 200

    bad = c.post(up, data={"channel_url": "https://www.youtube.com/@democreator", "email": "creator@example.com",
                           "niche": "x", "capacity": "1 video a week", "goal": "Views"},
                 files={"file": ("x.csv", b"foo,bar\n1,2\n", "text/csv")})
    assert bad.status_code == 400 and "doesn" in bad.text

    csv_path = Path(TMP) / "export.csv"
    write_sample(str(csv_path))
    data = {"channel_url": "https://www.youtube.com/@democreator", "email": "creator@example.com"}
    if tier == "premium":
        data.update(niche="home workouts", capacity="2 videos a week", goal="Subscribers")
    r = c.post(up, data=data, files={"file": ("export.csv", csv_path.read_bytes(), "text/csv")}, follow_redirects=False)
    assert r.status_code == 303, r.text
    status_url = r.headers["location"].split("?")[0]
    assert "being built" in c.get(status_url).text

    drain()
    job = db.q1("SELECT * FROM jobs WHERE report_token=?", (status_url.split("/")[-1],))
    assert job["status"] == "done", job["error"]
    assert job["csv_path"] is None and not db.q("SELECT key FROM blobs WHERE key LIKE 'upload/%'"), \
        "CSV must be deleted after report"
    rep = c.get(f"/r/{job['report_token']}")
    assert rep.status_code == 200 and "Top problems" in rep.text
    assert rep.text.count('class="prob"') == 5
    assert c.get(f"/r/{job['report_token']}/pdf").headers["content-type"] == "application/pdf"
    # the free host's disk is wiped on restart: the PDF is rebuilt from the stored audit
    (config.REPORT_DIR / f"{job['report_token']}.pdf").unlink()
    again = c.get(f"/r/{job['report_token']}/pdf")
    assert again.status_code == 200 and again.content[:4] == b"%PDF"
    a = db.q1("SELECT * FROM audit_log WHERE job_id=?", (job["id"],))
    assert a["numcheck_failures"] == 0 and a["prompt_version"]
    assert db.q1("SELECT COUNT(*) n FROM advice WHERE job_id=?", (job["id"],))["n"] == 5
    assert db.q1("SELECT COUNT(*) n FROM outcome_checks WHERE job_id=?", (job["id"],))["n"] == 3
    assert db.q1("SELECT COUNT(*) n FROM followups WHERE job_id=?", (job["id"],))["n"] == 2
    assert any("delivery" in p.name for p in outbox())
    if tier == "premium":
        assert "30-day plan" in rep.text and "60-day plan" in rep.text and "Week eight" in rep.text
        assert "Template mode" not in rep.text and "Title and thumbnail fixes" in rep.text
        assert "being made" in rep.text, "report must show the video due date"
        nar = json.loads(a["narrative_json"])
        assert {r["day"] for r in nar["schedule"]} <= set(range(1, 31)) and nar["schedule"][-1]["day"] == 30
        assert min(r["day"] for r in nar["schedule60"]) > 30 and nar["schedule60"][-1]["day"] == 60
        assert "within 2 days" in [p for p in outbox() if "delivery" in p.name][-1].read_text()
        assert db.q1("SELECT 1 FROM video_jobs WHERE job_id=? AND kind='walkthrough'", (job["id"],))
        import shutil
        if shutil.which("ffmpeg"):  # real render with silent audio (the voice model isn't needed offline)
            from creator_audit import video
            os.environ["VIDEO_TTS"] = "silent"
            assert video.process_due() == 1
            vj = db.q1("SELECT * FROM video_jobs WHERE job_id=?", (job["id"],))
            assert vj["status"] == "done", vj["error"]
            data = db.get_blob(vj["output_path"])
            assert data and data[4:8] == b"ftyp" and len(data) < 15_000_000
            rep = c.get(f"/r/{job['report_token']}")
            assert "<video" in rep.text and "being made" not in rep.text
            assert c.get(f"/v/{vj['id']}.mp4").headers["content-type"] == "video/mp4"
            assert any("video walkthrough is ready" in p.read_text() for p in outbox() if "delivery" in p.name)
    else:
        assert not db.q1("SELECT 1 FROM video_jobs WHERE job_id=?", (job["id"],)), "only Premium gets a video"
    return job


def test_numcheck_rejects_invented_numbers():
    from creator_audit import numcheck
    m = {"a": 4.31, "b": -45.2, "c": 1240, "t": "Top 7 Tips"}
    nums, tn = numcheck.allowed_values(m)
    assert numcheck.check_text("CTR is 4.3%, views fell 45.2% to 1,240. Top 7 Tips. Last 10 uploads.", nums, tn) == []
    assert numcheck.check_text("Your CTR is 6.8% and you got 3,999 views", nums, tn) == ["6.8", "3,999"]


def test_outreach_flow():
    c = TestClient(app)
    stats = outreach.daily_scan(3)
    assert stats["searches"] == 3 and stats["kept"] > 0, stats
    drain()
    p = db.q1("SELECT * FROM prospects WHERE stage='teaser_built' ORDER BY id LIMIT 1")
    assert p and p["teaser_score"] is not None
    t = c.get(f"/t/{p['teaser_token']}")
    assert t.status_code == 200 and "Unlock full public audit" in t.text and t.headers["x-robots-tag"].startswith("noindex")

    # review queue: a person adds the email the creator published on their own site
    r = c.post(f"/admin/prospects/{p['id']}/contact", auth=AUTH, follow_redirects=False,
               data={"email": "hello@creator-site.com", "source": "https://creator-site.com/contact", "approve": "1"})
    assert r.status_code == 303
    before = len(outbox())
    res = outreach.send_due(ignore_hours=True)
    assert res["sent"] >= 1, res
    mail = [x for x in outbox() if "outreach" in x.name][-1].read_text()
    assert "Unsubscribe" in mail and config.BUSINESS_ADDRESS.split(",")[0] in mail and "List-Unsubscribe" in mail
    assert len(outbox()) == before + res["sent"]

    e = db.q1("SELECT * FROM emails WHERE prospect_id=? AND kind='outreach'", (p["id"],))
    assert c.get(f"/o/{e['token']}.gif").status_code == 200
    r = c.get(f"/c/{e['token']}", params={"u": f"http://testserver/t/{p['teaser_token']}"}, follow_redirects=False)
    assert r.headers["location"].endswith(p["teaser_token"])
    assert c.get(f"/c/{e['token']}", params={"u": "https://evil.example"}, follow_redirects=False).headers["location"] == "http://testserver"
    assert db.q1("SELECT stage FROM prospects WHERE id=?", (p["id"],))["stage"] == "clicked"

    # no follow-up for clicked prospects; unlock purchase from the teaser
    r = c.post("/checkout", data={"tier": "unlock", "ref": p["teaser_token"], "email": "hello@creator-site.com"},
               follow_redirects=False)
    assert r.headers["location"].startswith("/status/")
    drain()
    rep = c.get(r.headers["location"], follow_redirects=True)
    assert "Full public-data audit".lower() in rep.text.lower() or "Unlock" in rep.text
    assert db.q1("SELECT stage FROM prospects WHERE id=?", (p["id"],))["stage"] == "paid"

    # unsubscribe -> DNC -> never emailed again
    p2 = db.q1("SELECT * FROM prospects WHERE stage='teaser_built' ORDER BY id LIMIT 1")
    outreach.set_contact(p2["id"], "other@site.com", "https://site.com", approve=True)
    outreach.send_due(ignore_hours=True)
    e2 = db.q1("SELECT * FROM emails WHERE prospect_id=?", (p2["id"],))
    assert "unsubscribed" in c.get(f"/u/{e2['token']}").text
    db.ex("UPDATE prospects SET stage='emailed', last_emailed_at='2000-01-01T00:00:00+00:00' WHERE id=?", (p2["id"],))
    outreach.send_due(ignore_hours=True)
    assert db.q1("SELECT COUNT(*) n FROM emails WHERE prospect_id=?", (p2["id"],))["n"] == 1
    assert db.q1("SELECT stage FROM prospects WHERE id=?", (p2["id"],))["stage"] == "unsubscribed"

    # admin pages render
    assert c.get("/admin", auth=AUTH).status_code == 200
    assert c.get("/admin/queue", auth=AUTH).status_code == 200
    assert c.get("/admin").status_code == 401


def test_scheduled_tasks():
    db.ex("UPDATE followups SET due_at='2000-01-01T00:00:00+00:00'")
    db.ex("UPDATE outcome_checks SET due_at='2000-01-01T00:00:00+00:00'")
    assert tasks.send_followups() >= 1
    assert tasks.run_outcome_checks() >= 1
    assert isinstance(tasks.advice_grades(), list)
    tasks.weekly_summary()
    assert any("weekly" in p.name for p in outbox())
    db.ex("UPDATE prospects SET api_data_fetched_at='2000-01-01T00:00:00+00:00'")
    res = outreach.purge_stale_api_data()
    assert res["deleted"] + res["scrubbed"] > 0


def test_video_worker_api_and_deletion():
    c = TestClient(app)
    j = db.q1("SELECT * FROM video_jobs WHERE status='pending' LIMIT 1")
    if j:
        got = c.post("/api/video-jobs/claim", headers={"X-Worker-Key": "vk"}).json()
        assert got["script"]
        c.post(f"/api/video-jobs/{got['id']}/done", headers={"X-Worker-Key": "vk"},
               files={"file": ("v.mp4", b"\x00\x00", "video/mp4")})
    assert c.post("/api/video-jobs/claim", headers={"X-Worker-Key": "nope"}).status_code == 401
    job = db.q1("SELECT * FROM jobs WHERE kind='audit' AND status='done' LIMIT 1")
    r = c.post("/delete-my-data", data={"report_link": f"http://testserver/r/{job['report_token']}"})
    assert "Done" in r.text
    assert c.get(f"/r/{job['report_token']}").status_code == 410


if __name__ == "__main__":
    test_csv_rejections()
    test_numcheck_rejects_invented_numbers()
    test_paid_flow("standard")
    test_paid_flow("premium")
    test_outreach_flow()
    test_scheduled_tasks()
    test_video_worker_api_and_deletion()
    print("ALL PASSED", "postgres" if db.is_pg() else "sqlite", TMP)
