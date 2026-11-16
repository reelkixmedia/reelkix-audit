"""Storage: SQLite locally/in tests, Postgres (Neon, free) in production when DATABASE_URL is set.

The rest of the code writes SQLite-style SQL (? placeholders, INSERT OR IGNORE, SUM(bool)); for Postgres
it is translated here, so both backends run the same queries and the same tests."""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from decimal import Decimal
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
    emails_sent INTEGER NOT NULL DEFAULT 0,
    last_emailed_at TEXT,
    contacted_via TEXT,                    -- email
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

-- Files kept in the database (uploaded CSVs until processed, mascot videos): the free host's disk is not kept
CREATE TABLE IF NOT EXISTS blobs (
    key TEXT PRIMARY KEY,
    data BLOB NOT NULL,
    created_at TEXT NOT NULL
);

-- Self-serve preview lookups, for rate limiting (each one costs a Google Places call)
CREATE TABLE IF NOT EXISTS lookups (
    id INTEGER PRIMARY KEY,
    ip TEXT,
    query TEXT,
    result TEXT,
    created_at TEXT NOT NULL
);

-- Paid-API call counter per month, so free tiers are never exceeded (Google Places)
CREATE TABLE IF NOT EXISTS api_calls (
    month TEXT NOT NULL,
    sku TEXT NOT NULL,
    n INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (month, sku)
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




def is_pg() -> bool:
    return bool(config.DATABASE_URL)


# ------------------------------------------------------------------ Postgres translation
class Row(dict):
    """Works like sqlite3.Row: row["col"], row[0], dict(row)."""

    def __getitem__(self, k):
        if isinstance(k, int):
            return list(self.values())[k]
        return super().__getitem__(k)


_INSERT_TABLE = re.compile(r"^\s*INSERT\s+(?:OR\s+IGNORE\s+)?INTO\s+(\w+)", re.I)
_SUM = re.compile(r"\bSUM\(([^()]*)\)", re.I)
NO_ID_TABLES = {"task_runs", "blobs", "api_calls"}


def pg_sql(sql: str) -> str:
    s = sql.replace("%", "%%").replace("?", "%s")
    s = _SUM.sub(lambda m: f"SUM(CAST(({m.group(1)}) AS INTEGER))", s)
    if re.search(r"INSERT\s+OR\s+IGNORE", s, re.I):
        s = re.sub(r"INSERT\s+OR\s+IGNORE", "INSERT", s, flags=re.I).rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    m = _INSERT_TABLE.match(s)
    if m and m.group(1).lower() not in NO_ID_TABLES and "RETURNING" not in s.upper():
        s = s.rstrip().rstrip(";") + " RETURNING id"
    return s


def pg_schema(sql: str) -> str:
    return (sql.replace("INTEGER PRIMARY KEY", "BIGSERIAL PRIMARY KEY")
               .replace(" REAL", " DOUBLE PRECISION").replace(" BLOB", " BYTEA"))


def _pg_params(params):
    if isinstance(params, dict):
        return {k: int(v) if isinstance(v, bool) else v for k, v in params.items()}
    return tuple(int(v) if isinstance(v, bool) else v for v in params)


def _row(r):
    if r is None:
        return None
    return Row({k: float(v) if isinstance(v, Decimal) else (bytes(v) if isinstance(v, memoryview) else v)
                for k, v in r.items()})


class _PgCursor:
    def __init__(self, cur, is_insert: bool):
        self.rowcount = cur.rowcount
        self._rows = None
        self.lastrowid = None
        if cur.description is not None:
            self._rows = [_row(r) for r in cur.fetchall()]
            if is_insert and self._rows and "id" in self._rows[0]:
                self.lastrowid = self._rows[0]["id"]

    def fetchall(self):
        return self._rows or []

    def fetchone(self):
        return self._rows[0] if self._rows else None


class PgConn:
    """Autocommit connection with one automatic reconnect (Neon closes idle connections)."""

    def __init__(self):
        self._c = None

    def _connect(self):
        import psycopg
        from psycopg.rows import dict_row
        self._c = psycopg.connect(config.DATABASE_URL, autocommit=True, row_factory=dict_row,
                                  connect_timeout=15, prepare_threshold=None)

    def _ready(self):
        if self._c is None or self._c.closed:
            self._connect()
        return self._c

    def execute(self, sql: str, params=()):
        import psycopg
        tsql = pg_sql(sql)
        is_insert = bool(_INSERT_TABLE.match(sql))
        for attempt in (1, 2):
            try:
                return _PgCursor(self._ready().execute(tsql, _pg_params(params)), is_insert)
            except psycopg.OperationalError:
                self._c = None
                if attempt == 2:
                    raise

    def executescript(self, sql: str) -> None:
        self._ready().execute(pg_schema(sql))

    def commit(self) -> None:  # autocommit: every statement is already committed
        pass

    def rollback(self) -> None:
        pass


def conn():
    c = getattr(_local, "conn", None)
    if c is None:
        if is_pg():
            c = PgConn()
        else:
            c = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    return c


MIGRATIONS = [
    "ALTER TABLE orders ADD COLUMN business_id INTEGER",
    "ALTER TABLE emails ADD COLUMN business_id INTEGER",
    "ALTER TABLE businesses ADD COLUMN source TEXT",  # scan | self_serve
]


def init() -> None:
    conn().executescript(SCHEMA)
    for m in MIGRATIONS:
        if is_pg():
            conn().execute(m.replace("ADD COLUMN", "ADD COLUMN IF NOT EXISTS"))
            continue
        try:
            conn().execute(m)
        except sqlite3.OperationalError:
            pass  # already applied
    conn().commit()


@contextmanager
def tx() -> Iterator[Any]:
    c = conn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise


def q(sql: str, params: tuple | dict = ()) -> list:
    return conn().execute(sql, params).fetchall()


def q1(sql: str, params: tuple | dict = ()):
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


# ------------------------------------------------------------------ files kept in the database
BLOB_PREFIX = "blob:"


def put_blob(key: str, data: bytes) -> str:
    ex("INSERT INTO blobs (key, data, created_at) VALUES (?,?,?) "
       "ON CONFLICT(key) DO UPDATE SET data=excluded.data, created_at=excluded.created_at", (key, data, iso()))
    return BLOB_PREFIX + key


def get_blob(ref: str) -> bytes | None:
    r = q1("SELECT data FROM blobs WHERE key=?", (ref.removeprefix(BLOB_PREFIX),))
    return bytes(r["data"]) if r else None


def del_blob(ref: str) -> None:
    ex("DELETE FROM blobs WHERE key=?", (ref.removeprefix(BLOB_PREFIX),))
