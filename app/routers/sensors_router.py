from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app import sensors
from app.db import get_session
from app.models import Printer, PrinterSensor

router = APIRouter(prefix="/api/sensors", tags=["sensors"])           # administrator only (see app.auth)
MAX_SENSORS = 100


def _json(session: Session, s: PrinterSensor, names: dict) -> dict:
    st = sensors._state.get(s.id) or {}
    return {"id": s.id, "printer_id": s.printer_id, "printer": names.get(s.printer_id), "entity_id": s.entity_id, "condition": s.condition, "threshold": s.threshold,
            "label": s.label, "value": st.get("value"), "alerting": st.get("alerting"), "error": st.get("error")}


@router.get("")
def list_sensors(session: Session = Depends(get_session)):
    from app.settings_store import get_setting
    names = {p.id: p.name for p in session.exec(select(Printer)).all()}
    return {"configured": bool(get_setting(session, "ha_url", "") and get_setting(session, "ha_token", "")),
            "sensors": [_json(session, s, names) for s in session.exec(select(PrinterSensor).order_by(PrinterSensor.id)).all() if s.printer_id in names]}


@router.post("")
def bind(payload: dict, session: Session = Depends(get_session)):
    if len(session.exec(select(PrinterSensor.id)).all()) >= MAX_SENSORS:
        raise HTTPException(400, f"At most {MAX_SENSORS} sensors")
    printer_id = payload.get("printer_id")
    if isinstance(printer_id, bool) or not isinstance(printer_id, int) or not session.get(Printer, printer_id):
        raise HTTPException(400, "Choose a printer")
    entity = payload.get("entity_id")
    if not isinstance(entity, str) or not sensors.ENTITY.match(entity.strip()):
        raise HTTPException(400, "A sensor name looks like binary_sensor.printer_door")
    condition = payload.get("condition")
    if condition not in sensors.CONDITIONS:
        raise HTTPException(400, "condition must be on, off, above or below")
    threshold = payload.get("threshold")
    if condition in ("above", "below"):
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise HTTPException(400, "Give the number it is compared with")
        threshold = float(threshold)
    else:
        threshold = None
    label = payload.get("label")
    if label is not None and not isinstance(label, str):
        raise HTTPException(400, "label must be text")
    s = PrinterSensor(printer_id=printer_id, entity_id=entity.strip(), condition=condition, threshold=threshold, label=(label or "").strip()[:60] or None)
    session.add(s)
    session.commit()
    session.refresh(s)
    return _json(session, s, {p.id: p.name for p in session.exec(select(Printer)).all()})


@router.delete("/{sensor_id}")
def unbind(sensor_id: int, session: Session = Depends(get_session)):
    s = session.get(PrinterSensor, sensor_id)
    if not s:
        raise HTTPException(404, "Not found")
    sensors._state.pop(sensor_id, None)
    session.delete(s)
    session.commit()
    return {"status": "deleted"}


@router.post("/test")
def test(payload: dict, session: Session = Depends(get_session)):
    """What Home Assistant says a sensor is right now (nothing is saved)."""
    try:
        return {"state": sensors.read_state(session, str(payload.get("entity_id") or "").strip())}
    except sensors.SensorError as e:
        raise HTTPException(502, str(e))


@router.post("/poll")
def poll_now(session: Session = Depends(get_session)):
    held = sensors.poll(session)
    return {"holding": held}
