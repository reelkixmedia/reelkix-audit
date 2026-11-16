"""SQLite storage. One file, WAL mode so the web server, worker and scheduler can share it."""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    stripe_session_id TEXT UNIQUE,
    tier TEXT NOT NULL,                -- standard | premium | unlock
    price_cents INTEGER NOT NULL,
    email TEXT,
    status TEXT NOT NULL DEFAULT 'paid',  -- paid | refunded
    upload_token TEXT UNIQUE,
    token_used INTEGER NOT NULL DEFAULT 0,
    teaser_id INTEGER,                 -- set when the sale came from an outreach teaser
    parent_order_id INTEGER,           -- set for re-audits / upgrades
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,                -- audit | teaser | unlock
    order_id INTEGER,
    teaser_id INTEGER,
    tier TEXT NOT NULL,
    channel_url TEXT,
    channel_id TEXT,
    channel_title TEXT,
    email TEXT,
    csv_path TEXT,
    goals_json TEXT,
    status TEXT NOT NULL DEFAULT 'queued',  -- queued | running | done | flagged | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    report_token TEXT UNIQUE,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

-- Audit log required by the spec (tracking and grading forward)
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL,
    channel_id TEXT,
    tier TEXT,
    metrics_json TEXT,
    score REAL,
    subscores_json TEXT,
    narrative_json TEXT,
    report_version TEXT,
    prompt_version TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    numcheck_failures INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS advice (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL,
    channel_id TEXT,
    fix_type TEXT NOT NULL,
    title TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outcome_checks (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL,
    channel_id TEXT,
    day_offset INTEGER NOT NULL,       -- 30 | 60 | 90
    due_at TEXT NOT NULL,
    done_at TEXT,
    baseline_vpu REAL,
    baseline_upw REAL,
    vpu REAL,
    upw REAL,
    UNIQUE(job_id, day_offset)
);

CREATE TABLE IF NOT EXISTS followups (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL,
    day_offset INTEGER NOT NULL,       -- 30 | 60
    due_at TEXT NOT NULL,
    sent_at TEXT,
    UNIQUE(job_id, day_offset)
);

-- Outreach -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS prospects (
    id INTEGER PRIMARY KEY,
    channel_id TEXT UNIQUE NOT NULL,
    channel_title TEXT,
    niche TEXT,
    subscribers INTEGER,
    last_upload_at TEXT,
    recent_median REAL,
    earlier_median REAL,
    stage TEXT NOT NULL DEFAULT 'scanned',  -- scanned | teaser_built | contact_found | approved | emailed | opened | clicked | paid | skipped | unsubscribed
    teaser_token TEXT UNIQUE,
    teaser_job_id INTEGER,
    teaser_score REAL,
    teaser_expires_at TEXT,
    contact_email TEXT,
    contact_source TEXT,               -- where a human/lead source found the email (required)
    approved_at TEXT,
    emails_sent INTEGER NOT NULL DEFAULT 0,
    last_emailed_at TEXT,
    opened_at TEXT,
    clicked_at TEXT,
    paid_at TEXT,
    api_data_fetched_at TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dnc (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE,
    channel_id TEXT,
    reason TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS dnc_channel ON dnc(channel_id);

CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY,
    token TEXT UNIQUE NOT NULL,        -- used in open pixel + click links
    kind TEXT NOT NULL,                -- delivery | upload_link | followup | outreach | outreach_followup | weekly | alert
    to_email TEXT NOT NULL,
    subject TEXT,
    prospect_id INTEGER,
    job_id INTEGER,
    order_id INTEGER,
    status TEXT NOT NULL DEFAULT 'sent',  -- sent | failed
    error TEXT,
    opened_at TEXT,
    clicked_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_log (
    id INTEGER PRIMARY KEY,
    niche TEXT,
    query TEXT,
    results INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS video_jobs (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                -- teaser_clip | walkthrough
    script TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | claimed | done | failed
    claimed_at TEXT,
    output_path TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_runs (
    name TEXT PRIMARY KEY,
    last_run_at TEXT
);

-- Local business scanner ------------------------------------------------
-- Google Maps terms: only the place ID is stored long-term. Everything else shown to
-- the business (name, hours, address...) is re-fetched from Places when needed.
CREATE TABLE IF NOT EXISTS businesses (
    id INTEGER PRIMARY KEY,
    place_id TEXT UNIQUE NOT NULL,
    category TEXT,
    city TEXT,
    stage TEXT NOT NULL DEFAULT 'built',   -- built | contact_found | approved | contacted | visited | paid | consult | skipped | optout | expired
    token TEXT UNIQUE NOT NULL,            -- private preview link /b/<token>, mock site /m/<token>
    score INTEGER,
    checks_json TEXT,                      -- our own audit: [{key, passed}] (no Google content)
    ai_mentioned INTEGER,                  -- NULL = not checked
    contact_email TEXT,
    contact_source TEXT,
    approved_at TEXT,
    postcard_id TEXT,
    postcard_sent_at TEXT,
    emails_sent INTEGER NOT NULL DEFAULT 0,
    last_emailed_at TEXT,
    contacted_via TEXT,                    -- postcard | email | both
    first_visit_at TEXT,
    visits INTEGER NOT NULL DEFAULT 0,
    consult_clicked_at TEXT,
    paid_at TEXT,
    expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS local_scan_log (
    id INTEGER PRIMARY KEY,
    category TEXT,
    city TEXT,
    query TEXT,
    found INTEGER,
    no_website INTEGER,
    kept INTEGER,
    created_at TEXT NOT NULL
);

-- Sites bought by businesses: content comes from the owner's own setup form
CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY,
    business_id INTEGER,
    order_id INTEGER,
    slug TEXT UNIQUE,
    category TEXT,
    content_json TEXT,
    status TEXT NOT NULL DEFAULT 'setup',  -- setup | live
    created_at TEXT NOT NULL,
    live_at TEXT
);

CREATE TABLE IF NOT EXISTS deletion_requests (
    id INTEGER PRIMARY KEY,
    email TEXT,
    report_token TEXT,
    status TEXT NOT NULL DEFAULT 'done',
    created_at TEXT NOT NULL
);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat(timespec="seconds")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def token(n: int = 24) -> str:
    return secrets.token_urlsafe(n)


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    return c


MIGRATIONS = [
    "ALTER TABLE orders ADD COLUMN business_id INTEGER",
    "ALTER TABLE emails ADD COLUMN business_id INTEGER",
]


def init() -> None:
    conn().executescript(SCHEMA)
    for m in MIGRATIONS:
        try:
            conn().execute(m)
        except sqlite3.OperationalError:
            pass  # already applied
    conn().commit()


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    c = conn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise


def q(sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
    return conn().execute(sql, params).fetchall()


def q1(sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
    return conn().execute(sql, params).fetchone()


def ex(sql: str, params: tuple | dict = ()) -> int:
    with tx() as c:
        cur = c.execute(sql, params)
        return cur.lastrowid


def insert(table: str, row: dict[str, Any]) -> int:
    cols = ", ".join(row)
    qs = ", ".join("?" for _ in row)
    return ex(f"INSERT INTO {table} ({cols}) VALUES ({qs})", tuple(row.values()))


def update(table: str, row_id: int, fields: dict[str, Any]) -> None:
    sets = ", ".join(f"{k}=?" for k in fields)
    ex(f"UPDATE {table} SET {sets} WHERE id=?", (*fields.values(), row_id))


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, sort_keys=True)
