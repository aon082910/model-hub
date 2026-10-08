"""Which release this is (set at build time; "dev" when run from source) and how to compare releases."""
import os
import re
from typing import Optional

VERSION = os.environ.get("MODELHUB_VERSION", "").strip() or "dev"


def parse(value: Optional[str]) -> Optional[tuple]:
    """'v2.9.0' or '2.9.0' -> (2, 9, 0); None if it is not a plain release number."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (value or "").strip())
    return tuple(int(x) for x in match.groups()) if match else None


def is_newer(latest: Optional[str], current: Optional[str] = None) -> bool:
    new, old = parse(latest), parse(current if current is not None else VERSION)
    return bool(new and old and new > old)
