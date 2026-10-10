"""A Telegram bot for Model Hub: it answers questions, and (if you allow it) pauses, resumes and cancels prints.

Only the chat ids you list in Settings (telegram chat id) are ever answered; everyone else is ignored without a reply. Questions (/status, /queue) are always answered to those chats;
the commands that change anything (/pause, /resume, /cancel) work only if "let the bot control printers" is switched on, and a cancel has to be confirmed with /confirm within a minute.
The bot asks Telegram for updates with long polling, so nothing needs to reach your server from outside. One status message can be kept up to date in place instead of sending many."""
import asyncio
import hashlib
import json
import logging
import time
from typing import Optional

import httpx
from sqlmodel import Session, select

from app import activity, channels, printers as printing, printwatch
from app.db import engine
from app.models import Model3D, Printer, QueueItem
from app.settings_store import get_setting, set_setting

logger = logging.getLogger("modelhub.telegram")
CONFIRM_SECONDS = 60
_pending: dict = {}                      # chat id -> (printer id, expires at) for a cancel waiting for /confirm
HELP = ("Model Hub bot\n/status: what every printer is doing\n/queue: what is waiting\n"
        "/pause NAME, /resume NAME, /cancel NAME (then /confirm): only if the bot may control printers\n/help")


def _s(session: Session, key: str) -> str:
    return (get_setting(session, key, "") or "").strip()


def enabled(session: Session) -> bool:
    return bool(_s(session, "telegram_token") and channels.telegram_chats(session))


def control_allowed(session: Session) -> bool:
    return _s(session, "telegram_control") == "true"


def call(session: Session, method: str, payload: Optional[dict] = None, timeout: float = 15) -> dict:
    r = httpx.post(f"{channels.TELEGRAM_API}/bot{_s(session, 'telegram_token')}/{method}", json=payload or {}, timeout=timeout)
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or not data.get("ok"):
        raise RuntimeError(str(data.get("description") or r.status_code)[:200])
    return data.get("result")


def reply(session: Session, chat: str, text: str, thread: Optional[str] = None) -> None:
    payload = {"chat_id": chat, "text": text[:3900]}
    if thread and str(thread).isdigit():
        payload["message_thread_id"] = int(thread)
    try:
        call(session, "sendMessage", payload)
    except Exception as e:
        logger.warning("Telegram reply failed: %s", e)


def status_text(session: Session) -> str:
    lines = []
    for p in session.exec(select(Printer).order_by(Printer.name)).all():
        st = printwatch.latest.get(p.id) or {}
        if p.out_of_service:
            lines.append(f"{p.name}: out of service")
        elif not st:
            lines.append(f"{p.name}: not seen yet")
        elif not st.get("online"):
            lines.append(f"{p.name}: offline")
        else:
            bits = [st.get("state") or "?"]
            if st.get("state") in ("printing", "paused") and st.get("progress") is not None:
                bits.append(f"{round(st['progress'])}%")
            if st.get("file") and st.get("state") in ("printing", "paused"):
                bits.append(str(st["file"]))
            lines.append(f"{p.name}: " + ", ".join(bits))
    waiting = len(session.exec(select(QueueItem.id).where(QueueItem.status == "queued")).all())
    return ("\n".join(lines) or "No printers yet.") + f"\nQueue: {waiting} waiting"


def queue_text(session: Session) -> str:
    items = session.exec(select(QueueItem).where(QueueItem.status.in_(["queued", "printing"])).order_by(QueueItem.position)).all()[:10]
    names = {m.id: m.filename for m in session.exec(select(Model3D).where(Model3D.id.in_({i.model_id for i in items} or {0}))).all()}
    return "\n".join(f"#{i.position} {names.get(i.model_id, 'model ' + str(i.model_id))} ({i.status})" for i in items) or "The queue is empty."


def find_printer(session: Session, text: str):
    """(printer, None) for a name that matches exactly one printer, else (None, why)."""
    wanted = (text or "").strip().lower()
    if not wanted:
        return None, "Say which printer, like /pause voron"
    printers = session.exec(select(Printer)).all()
    exact = [p for p in printers if p.name.lower() == wanted]
    found = exact or [p for p in printers if p.name.lower().startswith(wanted)]
    if len(found) == 1:
        return found[0], None
    return None, ("No printer is called that" if not found else "More than one printer fits: " + ", ".join(p.name for p in found))


def control(session: Session, printer: Printer, action: str) -> str:
    state = printing.status(printer.kind, printer.url, printer.api_key, printer.serial)
    if not state["online"]:
        return f"{printer.name} cannot be reached"
    running, paused = state["state"] == "printing", state["state"] == "paused"
    if (action == "pause" and not running) or (action == "resume" and not paused) or (action == "cancel" and not (running or paused)):
        return f"{printer.name} is {state['state']}, so it cannot be told to {action}"
    try:
        printing.control(printer.kind, printer.url, printer.api_key, printer.serial, action)
    except printing.PrinterError as e:
        return f"The printer refused: {e}"
    activity.record(session, "Telegram", "printer", f"{printer.name}: {action} sent")
    return f"{printer.name}: {action} sent"


def handle_text(session: Session, chat: str, text: str, now: Optional[float] = None) -> Optional[str]:
    """The reply to one message from an allowed chat (None for nothing)."""
    now = now if now is not None else time.time()
    words = (text or "").strip().split(None, 1)
    if not words or not words[0].startswith("/"):
        return None
    command = words[0].split("@")[0].lower()
    argument = words[1] if len(words) > 1 else ""
    if command in ("/start", "/help"):
        return HELP
    if command == "/status":
        return status_text(session)
    if command == "/queue":
        return queue_text(session)
    if command in ("/pause", "/resume", "/cancel", "/confirm"):
        if not control_allowed(session):
            return "The bot is not allowed to control printers (switch that on in Model Hub's Settings)."
        if command == "/confirm":
            pending = _pending.pop(chat, None)
            if not pending or pending[1] < now:
                return "Nothing is waiting to be confirmed."
            printer = session.get(Printer, pending[0])
            return control(session, printer, "cancel") if printer else "That printer is gone."
        printer, why = find_printer(session, argument)
        if not printer:
            return why
        if command == "/cancel":
            _pending[chat] = (printer.id, now + CONFIRM_SECONDS)
            return f"Cancel the print on {printer.name}? A cancelled print cannot be resumed. Send /confirm within a minute."
        return control(session, printer, command[1:])
    return "I do not know that command. /help"


def handle_update(session: Session, update: dict) -> Optional[str]:
    message = update.get("message") or update.get("channel_post") or {}
    chat = str((message.get("chat") or {}).get("id", ""))
    if chat not in channels.telegram_chats(session):
        return None                                       # strangers get no answer at all
    answer = handle_text(session, chat, message.get("text") or "")
    if answer:
        reply(session, chat, answer, message.get("message_thread_id"))
    return answer


def poll_once(session: Session, wait: int = 0) -> int:
    """Fetch and answer pending updates. Returns how many were handled."""
    offset = int(_s(session, "telegram_offset") or 0)
    updates = call(session, "getUpdates", {"offset": offset, "timeout": wait, "allowed_updates": ["message", "channel_post"]}, timeout=wait + 15)
    for update in updates or []:
        offset = max(offset, int(update.get("update_id", 0)) + 1)
        try:
            handle_update(session, update)
        except Exception as e:
            logger.warning("Telegram update failed: %s", e)
    if updates:
        set_setting(session, "telegram_offset", str(offset))
    return len(updates or [])


def _once() -> bool:
    with Session(engine) as session:
        if not enabled(session):
            return False
        try:
            poll_once(session, wait=20)
        except Exception as e:
            logger.info("Telegram polling: %s", e)
            time.sleep(10)
        return True


async def loop() -> None:
    """The background task that listens (started with the app; idle until a bot is set up)."""
    from starlette.concurrency import run_in_threadpool
    while True:
        try:
            active = await run_in_threadpool(_once)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Telegram loop")
            active = False
        if not active:
            await asyncio.sleep(15)


def update_status_message(session: Session) -> Optional[str]:
    """Keep one status message up to date in the first allowed chat instead of sending new ones (when switched on)."""
    if not enabled(session) or _s(session, "telegram_status") != "true":
        return None
    chat = channels.telegram_chats(session)[0]
    text = status_text(session)
    digest = hashlib.sha1(text.encode()).hexdigest()
    if _s(session, "telegram_status_hash") == digest and _s(session, "telegram_status_msg"):
        return "unchanged"
    payload = {"chat_id": chat, "text": text}
    topic = _s(session, "telegram_topic")
    try:
        message_id = _s(session, "telegram_status_msg")
        if message_id.isdigit():
            try:
                call(session, "editMessageText", {**payload, "message_id": int(message_id)})
                set_setting(session, "telegram_status_hash", digest)
                return "edited"
            except RuntimeError as e:
                if "not modified" in str(e):
                    set_setting(session, "telegram_status_hash", digest)
                    return "unchanged"                    # (otherwise the message is gone: send a new one)
        if topic.isdigit():
            payload["message_thread_id"] = int(topic)
        sent = call(session, "sendMessage", {**payload, "disable_notification": True})
        set_setting(session, "telegram_status_msg", str(sent["message_id"]))
        set_setting(session, "telegram_status_hash", digest)
        return "sent"
    except Exception as e:
        logger.info("Telegram status message: %s", e)
        return None
