"""Warn once when a print planned for the next two weeks needs more filament than its spool will have left.

The calendar already marks these; this tells you without opening it. Each entry is announced once, and again only if it
stopped being short (you restocked or changed the plan) and then became short again.
"""
import json
from datetime import date, timedelta

from sqlmodel import Session, select

from app import calendar_plan
from app.models import Model3D, QueueItem
from app.notify import notify_event
from app.settings_store import get_setting, set_setting

LOOKAHEAD_DAYS = 14


def current_short(session: Session) -> dict:
    """{queue entry id: description} for planned entries within the next two weeks (or overdue) that the spool cannot cover."""
    limit = (date.today() + timedelta(days=LOOKAHEAD_DAYS)).isoformat()
    found = {}
    for item_id, info in calendar_plan.filament_shortfalls(session).items():
        item = session.get(QueueItem, item_id)
        if not item or (item.planned_date or "") > limit:
            continue
        model = session.get(Model3D, item.model_id)
        found[item_id] = f"{(model.filename if model else 'a model')} on {item.planned_date}: {info['spool']} is short by {info['short_by']:g} g"
    return found


def check_and_notify(session: Session) -> list:
    """Notify about entries that became short since the last look. Returns their descriptions."""
    short = current_short(session)
    try:
        before = set(json.loads(get_setting(session, "plan_short_notified", "[]")))
    except ValueError:
        before = set()
    fresh = [text for key, text in short.items() if key not in before]
    set_setting(session, "plan_short_notified", json.dumps(sorted(short)))
    if fresh:
        shown = "; ".join(fresh[:5]) + (f" and {len(fresh) - 5} more" if len(fresh) > 5 else "")
        notify_event(session, "plan_short", "Model Hub: not enough filament for the plan", shown)
    return fresh
