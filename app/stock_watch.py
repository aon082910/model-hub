"""Warn once when a filament spool or a supply runs low, and again only after it was restocked and ran low again."""
import json

from sqlmodel import Session, select

from app.models import Filament, InventoryItem
from app.notify import notify_event
from app.settings_store import get_setting, set_setting

DEFAULT_LOW_FILAMENT_G = 100


def low_filament_threshold(session: Session) -> float:
    try:
        return max(0.0, float(get_setting(session, "low_filament_g", str(DEFAULT_LOW_FILAMENT_G))))
    except ValueError:
        return float(DEFAULT_LOW_FILAMENT_G)


def current_low(session: Session) -> dict:
    """{key: description} of everything that is low right now (filament threshold 0 switches spool warnings off)."""
    low = {}
    threshold = low_filament_threshold(session)
    if threshold > 0:
        for f in session.exec(select(Filament)).all():
            if f.remaining_g <= threshold:
                label = " ".join(x for x in (f.material, f.brand, f.color) if x)
                low[f"f:{f.id}"] = f"{label}: {f.remaining_g:g} g left"
    for item in session.exec(select(InventoryItem)).all():
        if item.min_quantity and item.quantity <= item.min_quantity:
            low[f"s:{item.id}"] = f"{item.name}: {item.quantity} left"
    return low


def check_and_notify(session: Session) -> list:
    """Notify about things that became low since the last look. Returns their descriptions."""
    low = current_low(session)
    try:
        before = set(json.loads(get_setting(session, "low_stock_notified", "[]")))
    except ValueError:
        before = set()
    fresh = [text for key, text in low.items() if key not in before]
    set_setting(session, "low_stock_notified", json.dumps(sorted(low)))
    if fresh:
        shown = "; ".join(fresh[:6]) + (f" and {len(fresh) - 6} more" if len(fresh) > 6 else "")
        notify_event(session, "low_stock", "Model Hub: running low", shown)
    return fresh
