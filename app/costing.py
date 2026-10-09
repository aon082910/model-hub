"""What a print costs and what to ask for it: filament, electricity, the machine's wear, an allowance for failed prints, and a margin.

Everything comes from your own numbers (Settings, Costs): the price per kWh and the printer's watts, what an hour of machine time is worth to you,
the share of prints that fail (by default your own record, once there are enough prints) and the margin you want on top."""
from typing import Optional

from sqlalchemy import func
from sqlmodel import Session, select

from app import learned
from app.models import Filament, PrintFile, PrintLog
from app.settings_store import get_setting

DEFAULT_WATTS = 150.0
DEFAULT_FAILURE_PCT = 5.0
MIN_ATTEMPTS_FOR_OWN_RATE = 10


def number(session: Session, key: str, default: Optional[float] = None) -> Optional[float]:
    try:
        value = float(get_setting(session, key, "") or "")
    except ValueError:
        return default
    return value if value >= 0 else default


def failure_pct(session: Session) -> tuple:
    """(percent, where it came from): the figure you set, else your own failure rate once there are enough prints, else 5%."""
    own = number(session, "cost_failure_pct")
    if own is not None:
        return own, "your setting"
    failed = session.exec(select(func.count()).select_from(PrintLog).where(PrintLog.outcome == "failed")).one()
    total = session.exec(select(func.count()).select_from(PrintLog)).one()
    if total >= MIN_ATTEMPTS_FOR_OWN_RATE:
        return round(failed / total * 100, 1), f"your own record ({failed} of {total} prints failed)"
    return DEFAULT_FAILURE_PCT, "a default (too few prints to know)"


def per_gram(session: Session, filament_id: Optional[int]) -> tuple:
    """(price per gram or None, where it came from)."""
    spool = session.get(Filament, filament_id) if filament_id else None
    if spool and spool.cost is not None and spool.spool_weight_g:
        return spool.cost / spool.spool_weight_g, "the spool's price"
    if spool:                                                # no price on this spool: the average of the other spools of the material
        others = [f for f in session.exec(select(Filament).where(Filament.material == spool.material)).all() if f.cost is not None and f.spool_weight_g]
        if others:
            return sum(f.cost / f.spool_weight_g for f in others) / len(others), f"the average {spool.material} spool"
    default = number(session, "cost_default_per_kg")
    if default is not None:
        return default / 1000, "the default price per kg"
    return None, "no price known"


def quote(session: Session, grams: float, minutes: float, filament_id: Optional[int] = None, quantity: int = 1) -> dict:
    """The breakdown of one print and of `quantity` of them."""
    rate, rate_from = per_gram(session, filament_id)
    filament = grams * rate if rate is not None else None
    hours = minutes / 60
    kwh = number(session, "cost_kwh_price")
    watts = number(session, "cost_printer_watts", DEFAULT_WATTS) or 0.0
    electricity = hours * watts / 1000 * kwh if kwh is not None else None
    machine = hours * (number(session, "cost_machine_per_hour", 0.0) or 0.0)
    subtotal = (filament or 0) + (electricity or 0) + machine
    pct, pct_from = failure_pct(session)
    allowance = subtotal * pct / 100
    cost = subtotal + allowance
    margin = number(session, "cost_margin_pct", 0.0) or 0.0
    price = cost * (1 + margin / 100)
    unit = {"filament": filament, "electricity": electricity, "machine": machine, "failures": allowance, "cost": cost, "price": price}
    rounded = {k: (round(v, 2) if v is not None else None) for k, v in unit.items()}
    return {"grams": round(grams, 1), "minutes": round(minutes), "quantity": quantity, "unit": rounded,
            "total": {k: (round(v * quantity, 2) if v is not None else None) for k, v in unit.items()},
            "margin_pct": margin, "failure_pct": pct, "failure_from": pct_from, "filament_from": rate_from,
            "missing": [name for name, value in (("a price for the filament", filament), ("the price per kWh", electricity)) if value is None]}


def defaults_for(session: Session, model_id: int) -> dict:
    """Grams and minutes to start from for a model: its kept sliced file, else its last print; minutes from what its own prints say."""
    grams = None
    for kept in session.exec(select(PrintFile).where(PrintFile.model_id == model_id).order_by(PrintFile.created_at.desc(), PrintFile.id.desc())).all():
        if kept.est_grams:
            grams = float(kept.est_grams)
            break
    last = session.exec(select(PrintLog).where(PrintLog.model_id == model_id, PrintLog.grams.is_not(None), PrintLog.outcome.is_(None))
                        .order_by(PrintLog.printed_at.desc(), PrintLog.id.desc())).first()
    if grams is None and last:
        grams = float(last.grams)
    minutes = learned.suggest(session, model_id)["minutes"]
    return {"grams": grams, "minutes": minutes, "filament_id": last.filament_id if last else None}
