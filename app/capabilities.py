"""What a printer can do, and what a print needs: free-form labels such as plate=textured or enclosed=yes.

A printer carries any labels you give it (`plate=smooth`, `enclosed=yes`, `nozzle=0.6`, `swapper=farmloop`). A queue entry can list what it needs the same way (`plate=textured`). A printer
that lacks a needed label, or has a different value, is never suggested for the entry and cannot be sent it. Names and values are compared without regard to case. The labels mean whatever you
decide: Model Hub only compares them."""
import json
import re
from typing import Optional

MAX_LABELS = 12
_KEY = re.compile(r"^[a-z0-9_]{1,24}$")
_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.+-]{0,39}$")


class CapabilityError(ValueError):
    """A label that cannot be used; the message is safe to show."""


def clean(value) -> Optional[dict]:
    """A dict of labels from a dict or from text like "plate=textured, enclosed=yes"; None for none. Raises CapabilityError."""
    if value in (None, "", {}, []):
        return None
    if isinstance(value, str):
        pairs = {}
        for part in re.split(r"[,;\n]+", value):
            if not part.strip():
                continue
            key, sep, val = part.partition("=")
            if not sep:
                raise CapabilityError(f'"{part.strip()[:30]}" needs a value, like plate=textured')
            pairs[key] = val
        value = pairs
    if not isinstance(value, dict) or len(value) > MAX_LABELS:
        raise CapabilityError(f"Give at most {MAX_LABELS} labels like plate=textured")
    out = {}
    for key, val in value.items():
        k = str(key).strip().lower().replace(" ", "_")
        v = val if isinstance(val, str) else ("yes" if val is True else "no" if val is False else str(val))
        v = v.strip()
        if not _KEY.match(k):
            raise CapabilityError("A label name is 1 to 24 letters, numbers or underscores")
        if not _VALUE.match(v):
            raise CapabilityError("A label value is up to 40 letters, numbers, spaces or . _ + -")
        out[k] = v
    return out or None


def load(text: Optional[str]) -> dict:
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def dump(labels: Optional[dict]) -> Optional[str]:
    return json.dumps(labels, sort_keys=True) if labels else None


def missing(needs: dict, has: dict) -> list:
    """What a printer lacks for this need, as ["plate: textured", ...]; empty when it fits."""
    have = {k.lower(): str(v).strip().lower() for k, v in (has or {}).items()}
    return [f"{k}: {v}" for k, v in (needs or {}).items() if have.get(k.lower()) != str(v).strip().lower()]


def text(labels: Optional[dict]) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted((labels or {}).items()))
