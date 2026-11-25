"""Log privacy. The repo's Actions logs are PUBLIC, so nothing about prospects or customers may reach them.

In GitHub Actions: exceptions are logged as their type (plus HTTP status), never their message or source lines,
and any web address or email left in a line is blanked. Elsewhere (Render, local) logs are unchanged.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import traceback

_URL = re.compile(r"(?:https?://|www\.)[^\s'\"<>]+", re.I)
_EMAIL = re.compile(r"[\w.+%-]+@[\w-]+(?:\.[\w-]+)+")
_HOST = re.compile(r"host='[^']*'")
_PREVIEW = re.compile(r"/b/[\w-]+")


def public_logs() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def redact(text: str) -> str:
    for rx, to in ((_URL, "[url]"), (_EMAIL, "[email]"), (_HOST, "host=[host]"), (_PREVIEW, "/b/[token]")):
        text = rx.sub(to, text)
    return text


def describe(e: BaseException) -> str:
    code = getattr(getattr(e, "response", None), "status_code", None)
    return type(e).__name__ + (f" HTTP {code}" if code else "")


class SafeFormatter(logging.Formatter):
    def format(self, record):
        if isinstance(record.args, tuple):
            record = logging.makeLogRecord(record.__dict__)
            record.args = tuple(describe(a) if isinstance(a, BaseException) else a for a in record.args)
        record.exc_text = None
        return redact(super().format(record))

    def formatException(self, ei):
        frames = traceback.extract_tb(ei[2])
        return describe(ei[1]) + " at " + " > ".join(f"{os.path.basename(f.filename)}:{f.lineno}" for f in frames)


def setup() -> None:
    fmt = "%(asctime)s %(name)s %(levelname)s %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt)
    if public_logs():
        for h in logging.getLogger().handlers:
            h.setFormatter(SafeFormatter(fmt))
        sys.excepthook = lambda t, e, tb: print(SafeFormatter().formatException((t, e, tb)), file=sys.stderr)
