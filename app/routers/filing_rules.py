from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session, select

from app import activity, filing
from app.db import get_session
from app.models import FilingRule, Model3D

router = APIRouter(prefix="/api/filing-rules", tags=["filing-rules"])

MAX_RULES = 200


def _json(rule: FilingRule) -> dict:
    return {"id": rule.id, "field": rule.field, "match": rule.match, "action": rule.action, "value": rule.value, "enabled": rule.enabled}


def _clean(payload: dict, existing: bool = False) -> dict:
    out = {}
    if not existing or "field" in payload:
        if payload.get("field") not in filing.FIELDS:
            raise HTTPException(400, "field must be one of: " + ", ".join(filing.FIELDS))
        out["field"] = payload["field"]
    if not existing or "match" in payload:
        match = payload.get("match")
        if not isinstance(match, str) or len(match.strip()) < filing.MIN_MATCH or len(match.strip()) > 80:
            raise HTTPException(400, f"The text to look for must be {filing.MIN_MATCH}-80 characters")
        out["match"] = match.strip()
    if not existing or "action" in payload:
        if payload.get("action") not in filing.ACTIONS:
            raise HTTPException(400, "action must be one of: " + ", ".join(filing.ACTIONS))
        out["action"] = payload["action"]
    if not existing or "value" in payload:
        value = payload.get("value")
        if not isinstance(value, str) or not value.strip():
            raise HTTPException(400, "Give the tag or the collection name")
        out["value"] = value.strip().lower()[:40] if (payload.get("action") == "add_tag") else value.strip()[:80]
    if "enabled" in payload:
        if not isinstance(payload["enabled"], bool):
            raise HTTPException(400, "enabled must be true or false")
        out["enabled"] = payload["enabled"]
    return out


@router.get("")
def list_rules(session: Session = Depends(get_session)):
    return {"rules": [_json(r) for r in session.exec(select(FilingRule).order_by(FilingRule.id)).all()],
            "fields": list(filing.FIELDS), "actions": list(filing.ACTIONS)}


@router.post("")
def add_rule(payload: dict, session: Session = Depends(get_session)):
    if len(session.exec(select(FilingRule.id)).all()) >= MAX_RULES:
        raise HTTPException(400, f"At most {MAX_RULES} rules")
    rule = FilingRule(**_clean(payload))
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return _json(rule)


@router.patch("/{rule_id}")
def update_rule(rule_id: int, payload: dict, session: Session = Depends(get_session)):
    rule = session.get(FilingRule, rule_id)
    if not rule:
        raise HTTPException(404, "Not found")
    changes = _clean({**payload, "action": payload.get("action", rule.action)}, existing=True)
    for key, value in changes.items():
        setattr(rule, key, value)
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return _json(rule)


@router.delete("/{rule_id}")
def delete_rule(rule_id: int, session: Session = Depends(get_session)):
    rule = session.get(FilingRule, rule_id)
    if not rule:
        raise HTTPException(404, "Not found")
    session.delete(rule)
    session.commit()
    return {"status": "deleted"}


@router.post("/apply")
def apply_rules(payload: dict, request: Request, session: Session = Depends(get_session)):
    """Run the rules over every model linked to a listing. dry_run=true only counts what would change."""
    dry = payload.get("dry_run") is True
    models = session.exec(select(Model3D).where(Model3D.source_provider.is_not(None))).all()
    result = filing.apply(session, models, dry_run=dry)
    activity_id = None
    if not dry and result["undo"]:
        entry = activity.record(session, activity.actor_of(request), "filing", f"Ran the filing rules: {result['changed']} change(s) over {result['models_checked']} models",
                                undo=result["undo"])
        activity_id = entry.id
    return {"rules": result["rules"], "models_checked": result["models_checked"], "changed": result["changed"],
            "dry_run": dry, "activity_id": activity_id}
