"""A summary of the week, sent to the notification webhook once every seven days when you switch it on.

Opt-in (Settings, Notifications): without it nothing is sent. Switching it on starts the first week from that day, so you do not get a
summary of a week that was never watched.
"""
from datetime import date, datetime, timedelta

from sqlmodel import Session, select

from app import calendar_plan, maintenance, print_outcomes, stock_watch, update_check
from app.models import Filament, PrintLog, QueueItem
from app.notify import notify_event
from app.settings_store import get_setting, set_setting

INTERVAL_DAYS = 7


def enabled(session: Session) -> bool:
    return get_setting(session, "weekly_summary", "") == "true"


def build(session: Session, now: datetime = None) -> str:
    """The text of the summary of the last seven days (and a look at the next seven)."""
    now = now or datetime.utcnow()
    since = now - timedelta(days=INTERVAL_DAYS)
    logs = session.exec(select(PrintLog).where(PrintLog.printed_at >= since)).all()
    good = [l for l in logs if not print_outcomes.is_failed(l)]
    failed = [l for l in logs if print_outcomes.is_failed(l)]
    spools = {f.id: f for f in session.exec(select(Filament)).all()}
    grams = sum(float(l.grams or 0) for l in good)
    cost = sum(float(l.grams or 0) * spools[l.filament_id].cost / spools[l.filament_id].spool_weight_g
               for l in good if l.filament_id in spools and spools[l.filament_id].cost is not None and spools[l.filament_id].spool_weight_g)
    hours = sum(float(l.minutes or 0) for l in good) / 60
    lines = [f"Last week: {len(good)} print{'' if len(good) == 1 else 's'}" + (f", {hours:.1f} h of printing" if hours else "")
             + (f", {grams:.0f} g of filament" if grams else "") + (f" (${cost:.2f})" if cost else "") + "."]
    if failed:
        reasons: dict = {}
        for l in failed:
            if l.failure_reason:
                reasons[l.failure_reason] = reasons.get(l.failure_reason, 0) + 1
        top = max(sorted(reasons), key=lambda r: reasons[r]) if reasons else None
        lines.append(f"{len(failed)} failed" + (f", most often: {print_outcomes.label(top)}." if top else "."))
    waiting = session.exec(select(QueueItem).where(QueueItem.status == "queued")).all()
    if waiting:
        minutes = sum(q.estimated_minutes or 0 for q in waiting)
        lines.append(f"{len(waiting)} print{'' if len(waiting) == 1 else 's'} waiting in the queue" + (f" (about {minutes / 60:.1f} h)" if minutes else "") + ".")
    today = date.today()
    ahead = [q for q in session.exec(select(QueueItem).where(QueueItem.status.in_(calendar_plan.OPEN), QueueItem.planned_date.is_not(None))).all()
             if today.isoformat() <= (q.planned_date or "") <= (today + timedelta(days=INTERVAL_DAYS)).isoformat()]
    if ahead:
        short = [q for q in ahead if q.id in calendar_plan.filament_shortfalls(session)]
        lines.append(f"{len(ahead)} planned for the coming week" + (f", {len(short)} of them short of filament" if short else "") + ".")
    low = stock_watch.current_low(session)
    if low:
        lines.append("Running low: " + "; ".join(list(low.values())[:5]) + ".")
    due = [f"{r['printer']}: {r['name']}" for r in maintenance.overview(session) if r["status"] in ("due", "soon")]
    if due:
        lines.append("Maintenance: " + "; ".join(due[:5]) + ".")
    if update_check.status(session).get("update_available"):
        lines.append("A newer Model Hub release is available.")
    return "\n".join(lines)


def send(session: Session) -> str:
    text = build(session)
    notify_event(session, "weekly_summary", "Model Hub: your week", text)
    return text


def scheduled(session: Session) -> bool:
    """Called regularly: sends the summary when it is switched on and seven days have passed since the last one."""
    if not enabled(session):
        return False
    last = get_setting(session, "weekly_summary_last", "")
    today = date.today()
    if not last:
        set_setting(session, "weekly_summary_last", today.isoformat())
        return False
    try:
        due = (today - date.fromisoformat(last)).days >= INTERVAL_DAYS
    except ValueError:
        due = True
    if not due:
        return False
    send(session)
    set_setting(session, "weekly_summary_last", today.isoformat())
    return True
