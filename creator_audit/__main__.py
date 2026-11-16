"""CLI:  python -m creator_audit <command>

  serve        web/API server (uvicorn) on $PORT (default 8000)
  worker       audit job worker
  scheduler    daily scanner, outreach sender, follow-ups, outcome checks, weekly summary, purges
  all          serve + worker + scheduler in one process (local / always-on server)
  web          serve + worker (free host that sleeps when idle; scheduled work runs from `cron`)
  cron [MIN]   one scheduler pass, then work through queued jobs for up to MIN minutes (default 8).
               Run by the GitHub Action every few hours.
  scan [N]     run the outreach scanner once with N searches
  send         send due outreach emails now (ignores send hours)
  local-scan [N]    run the local business scanner once with N Places searches
  local-send        send due emails to local businesses now
  sample-csv PATH   write a fake YouTube Studio export for testing
"""
from __future__ import annotations

import logging
import os
import sys
import threading

from . import db


def main(argv: list[str]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cmd = argv[0] if argv else "all"
    db.init()
    if cmd in ("serve", "all", "web"):
        import uvicorn
        if cmd in ("all", "web"):
            from .pipeline import worker_loop
            threading.Thread(target=worker_loop, daemon=True, name="worker").start()
        if cmd == "all":
            from .tasks import scheduler_loop
            threading.Thread(target=scheduler_loop, daemon=True, name="scheduler").start()
        uvicorn.run("creator_audit.app:app", host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", 8000)),
                    proxy_headers=True, forwarded_allow_ips="*")
    elif cmd == "cron":
        import time
        from .pipeline import process_one
        from .tasks import tick
        tick()
        deadline = time.time() + 60 * float(argv[1] if len(argv) > 1 else 8)
        n = 0
        while time.time() < deadline and process_one():
            n += 1
        print(f"cron: scheduler pass done, {n} job(s) processed")
    elif cmd == "worker":
        from .pipeline import worker_loop
        worker_loop()
    elif cmd == "scheduler":
        from .tasks import scheduler_loop
        scheduler_loop()
    elif cmd == "scan":
        from .outreach import daily_scan
        print(daily_scan(int(argv[1]) if len(argv) > 1 else None))
    elif cmd == "send":
        from .outreach import send_due
        print(send_due(ignore_hours=True))
    elif cmd == "local-scan":
        from .local import daily_scan as local_scan
        print(local_scan(int(argv[1]) if len(argv) > 1 else None))
    elif cmd == "local-send":
        from .local import send_due as local_send
        print(local_send(ignore_hours=True))
    elif cmd == "sample-csv":
        from .sample import write_sample
        print(write_sample(argv[1] if len(argv) > 1 else "sample_studio_export.csv"))
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1:])
