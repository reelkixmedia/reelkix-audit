"""Run both test files against a real Postgres (the production database type) on this PC.

    pip install pgserver "psycopg[binary]"
    python tests/run_pg.py

pgserver starts a throwaway Postgres 16; each test file gets its own empty database."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pgserver
import psycopg

HERE = Path(__file__).resolve().parent


def main() -> int:
    srv = pgserver.get_server(tempfile.mkdtemp(prefix="pg_"), cleanup_mode="stop")
    base = srv.get_uri()
    failed = 0
    try:
        for i, test in enumerate(("test_e2e.py", "test_local.py")):
            name = f"audit_test_{i}"
            with psycopg.connect(base, autocommit=True) as c:
                c.execute(f"DROP DATABASE IF EXISTS {name}")
                c.execute(f"CREATE DATABASE {name}")
            uri = base.rsplit("/", 1)[0] + "/" + name
            r = subprocess.run([sys.executable, str(HERE / test)], env={**os.environ, "TEST_DATABASE_URL": uri})
            print(f"[postgres] {test}: {'ok' if r.returncode == 0 else 'FAILED'}")
            failed += r.returncode != 0
    finally:
        srv.cleanup()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
