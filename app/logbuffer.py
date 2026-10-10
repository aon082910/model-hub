"""The last few hundred log lines, kept in memory so they can be read in Settings and put in a support bundle. Secrets that could end up in a log line are blanked."""
import logging
import re
from collections import deque
from datetime import datetime
from typing import Optional

MAX_LINES = 1000
_buffer: deque = deque(maxlen=MAX_LINES)
_installed = False

_SECRETS = [
    (re.compile(r"(?i)(authorization:\s*)(bearer|basic)\s+\S+"), r"\1\2 ***"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 ***"),
    (re.compile(r"(?i)([?&](?:token|key|api_?key|apikey|access_?token|secret|password|code|signature)=)[^&\s\"']+"), r"\1***"),
    (re.compile(r"(?i)\b(mh_[A-Za-z0-9_-]{8,})"), "mh_***"),
    (re.compile(r"(?i)(password|passwd|secret|token|api[-_ ]?key)(\s*[=:]\s*)\S+"), r"\1\2***"),
    (re.compile(r"/(?:status|wall|quote|share|dl)/[A-Za-z0-9_-]{16,}"), "/***"),
    (re.compile(r"/hooks/shop/[A-Za-z0-9_-]{16,}"), "/hooks/shop/***"),
    (re.compile(r"://[^/\s:@]+:[^/\s@]+@"), "://***:***@"),
]


def redact(text: str) -> str:
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


class _Handler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _buffer.append({"at": datetime.utcfromtimestamp(record.created).isoformat(timespec="seconds") + "Z", "level": record.levelname, "logger": record.name,
                            "message": redact(record.getMessage())[:2000]})
        except Exception:
            pass


def install() -> None:
    global _installed
    if _installed:
        return
    root = logging.getLogger()
    root.addHandler(_Handler(level=logging.INFO))
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)                  # (the log shown in Settings starts at "info")
    _installed = True


LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


def lines(level: str = "INFO", query: Optional[str] = None, limit: int = 200) -> list:
    floor = LEVELS.get((level or "INFO").upper(), 20)
    wanted = (query or "").strip().lower()
    out = [r for r in _buffer if LEVELS.get(r["level"], 20) >= floor and (not wanted or wanted in r["message"].lower() or wanted in r["logger"].lower())]
    return out[-max(1, min(limit, MAX_LINES)):]


def text(level: str = "INFO", limit: int = MAX_LINES) -> str:
    return "\n".join(f"{r['at']} {r['level']:<8} {r['logger']}: {r['message']}" for r in lines(level, None, limit))
