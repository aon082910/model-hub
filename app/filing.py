"""Filing rules: tag or file a model automatically from what its listing says.

A rule says: when the (field) of a linked model's listing contains (text), add a tag, or add the model to a collection
(created when it does not exist). Rules run when a model is linked to a listing and, on request, over every linked model.
They only ever add (a tag, a collection link): nothing is removed, and the whole run can be undone from Recent changes.
"""
import json
import logging
from typing import Optional

from sqlmodel import Session, select

from app.models import Collection, FilingRule, Model3D

logger = logging.getLogger("modelhub.filing")

FIELDS = ("category", "tag", "title", "designer")
ACTIONS = ("add_tag", "add_collection")
MIN_MATCH = 2


def haystacks(model: Model3D) -> dict:
    try:
        tags = json.loads(model.source_tags) if model.source_tags else []
    except ValueError:
        tags = []
    return {"category": [model.source_category or ""], "tag": [str(t) for t in tags if isinstance(t, str)],
            "title": [model.source_title or "", model.filename or ""], "designer": [model.designer or ""]}


def rule_matches(rule: FilingRule, model: Model3D) -> bool:
    needle = rule.match.strip().lower()
    return bool(needle) and any(needle in text.lower() for text in haystacks(model).get(rule.field, []))


def plan(rules: list, models: list) -> dict:
    """{(action, value): [model ids]} for the enabled rules that match."""
    out = {}
    for rule in rules:
        if not rule.enabled:
            continue
        for model in models:
            if rule_matches(rule, model):
                ids = out.setdefault((rule.action, rule.value), [])
                if model.id not in ids:
                    ids.append(model.id)
    return out


def _collection_id(session: Session, name: str, create: bool) -> Optional[int]:
    col = session.exec(select(Collection).where(Collection.name == name)).first()
    if col:
        return col.id
    if not create:
        return None
    col = Collection(name=name)
    session.add(col)
    session.flush()
    return col.id


def apply(session: Session, models: list, dry_run: bool = False) -> dict:
    """Run every enabled rule over these models. Returns counts and (unless dry_run) an undo spec. Commits unless dry_run."""
    from app.routers import bulk
    rules = session.exec(select(FilingRule).order_by(FilingRule.id)).all()
    result = {"rules": len([r for r in rules if r.enabled]), "models_checked": len(models), "changed": 0, "undo": None, "dry_run": dry_run}
    specs = []
    for (action, value), ids in plan(rules, models).items():
        if action == "add_collection":
            target = _collection_id(session, value, create=not dry_run)
            if target is None:                               # dry run: the collection does not exist yet, so everything would be new
                result["changed"] += len(ids)
                continue
            value = target
        done = bulk.apply(session, action, value, ids)
        result["changed"] += len(done["changed_ids"])
        if done["changed_ids"]:
            specs.append(bulk.undo_spec(action, value, done))
    if dry_run:
        session.rollback()
        return result
    session.commit()
    result["undo"] = {"kind": "multi", "specs": specs} if specs else None
    return result


def on_linked(session: Session, model: Model3D) -> None:
    """Called after a model is linked to a listing."""
    if session.exec(select(FilingRule.id).where(FilingRule.enabled == True).limit(1)).first() is None:   # noqa: E712
        return
    result = apply(session, [model])
    if result["undo"]:
        from app import activity
        activity.record(session, "filing rules", "filing", f"Filing rules filed {model.filename}", undo=result["undo"])
