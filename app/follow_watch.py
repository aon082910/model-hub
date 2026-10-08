"""Look for new uploads from followed designers on a schedule and tell you about them."""
import logging

from sqlmodel import Session, select

from app import sources
from app.models import FollowedDesigner
from app.notify import notify_event

logger = logging.getLogger("modelhub.followwatch")


def check_and_notify(session: Session) -> int:
    from app.routers.designers import MAX_PER_CHECK, check_designer
    designers = session.exec(select(FollowedDesigner).order_by(FollowedDesigner.last_checked_at)).all()[:MAX_PER_CHECK]
    if not designers:
        return 0
    credentials = sources.load_credentials(session)
    found = {}
    for designer in designers:
        try:
            new = check_designer(session, designer, credentials)
            if new:
                found[designer.name or designer.handle] = new
        except sources.SourceError as e:
            designer.last_error = str(e)[:300]
            session.add(designer)
        session.commit()
    total = sum(found.values())
    if total:
        who = ", ".join(f"{name} ({n})" for name, n in list(found.items())[:5])
        notify_event(session, "new_uploads", "Model Hub: new uploads", f"{total} new upload{'s' if total != 1 else ''} from {who}.")
    return total
