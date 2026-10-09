import io
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlmodel import Session
from app.db import get_session
from app.settings_store import all_settings, set_setting
from app.auth import ACCESS_SECRETS, RESERVED_SETTING_KEYS, ensure_extension_api_key
from app.sources import secret_setting_keys
import secrets

router = APIRouter(prefix="/api/settings", tags=["settings"])

# Keys never echoed back in plaintext to the frontend after being set
SECRET_KEYS = {"ai_api_key", "mqtt_password", "offsite_password", "ha_token", "metrics_token", "telegram_token", "pushover_token", "pushover_user", "gotify_token", "matrix_token", "bark_key"} | set(ACCESS_SECRETS) | secret_setting_keys()   # site tokens / API keys


@router.get("")
def get_settings(session: Session = Depends(get_session)):
    data = all_settings(session)
    for k in RESERVED_SETTING_KEYS:
        data.pop(k, None)
    for k in SECRET_KEYS:
        if data.get(k):
            data[k] = "********"
    return data


@router.put("")
def update_settings(payload: dict, request: Request, session: Session = Depends(get_session)):
    changed = []
    for k, v in payload.items():
        if k in RESERVED_SETTING_KEYS:
            continue  # these have their own dedicated, more-restricted endpoints
        if v == "********":
            continue  # unchanged secret, skip
        set_setting(session, k, str(v))
        changed.append(k)
    if changed:
        from app import activity
        shown = ", ".join(changed[:8]) + (f" and {len(changed) - 8} more" if len(changed) > 8 else "")
        activity.record(session, activity.actor_of(request), "settings", f"Changed settings: {shown}")      # names only, never values
    return {"status": "ok"}


@router.get("/metrics")
def metrics_settings(request: Request, session: Session = Depends(get_session)):
    from app import metrics
    from app.settings_store import get_setting
    base = str(request.base_url).rstrip("/")
    return {"enabled": metrics.enabled(session), "token_set": bool(get_setting(session, "metrics_token", "")), "scrape_url": base + "/metrics"}


@router.get("/metrics/grafana.json")
def grafana_dashboard():
    """The Grafana dashboard for these metrics, as a file to import."""
    from fastapi.responses import JSONResponse
    from app import metrics
    return JSONResponse(metrics.grafana_dashboard(), headers={"Content-Disposition": 'attachment; filename="modelhub-grafana.json"'})


@router.get("/notify-events")
def notify_events(session: Session = Depends(get_session)):
    """The kinds of notification, and which are switched on."""
    from app.notify import EVENTS, event_enabled
    from app.notify import PLACEHOLDERS
    from app.settings_store import get_setting
    return {"events": [{"id": k, "label": v, "enabled": event_enabled(session, k), "text": get_setting(session, f"notify_text_{k}", "") or ""} for k, v in EVENTS.items()],
            "placeholders": list(PLACEHOLDERS)}


@router.post("/notify-test")
def notify_test(session: Session = Depends(get_session)):
    """Send a test message to the webhook, to check it is set up right."""
    from app import channels
    from app.notify import notify
    from app.settings_store import get_setting
    if not get_setting(session, "notify_webhook_url") and not channels.configured(session):
        raise HTTPException(400, "Save a webhook URL, or set up Telegram, Pushover, Gotify, Matrix or Bark, first")
    if not notify(session, "Model Hub: test", "If you can read this, notifications work."):
        raise HTTPException(502, "Nothing accepted the message (check the addresses and keys)")
    return {"status": "sent", "channels": (["webhook"] if get_setting(session, "notify_webhook_url") else []) + channels.configured(session)}


@router.post("/weekly-test")
def weekly_test(session: Session = Depends(get_session)):
    """Send the summary of the last week now (to see what it looks like, and to check the webhook)."""
    from app import weekly
    return {"status": "sent", "message": weekly.send(session)}


@router.post("/mqtt-test")
def mqtt_test(session: Session = Depends(get_session)):
    """Send one test message to the MQTT broker, to check the settings."""
    from app import mqtt_publish
    try:
        return {"status": "sent", "message": mqtt_publish.send_test(session)}
    except mqtt_publish.MqttError as e:
        raise HTTPException(502, str(e))


@router.get("/extension-key")
def get_extension_key(session: Session = Depends(get_session)):
    """Session-cookie only (RESERVED_SETTING_KEYS keeps it out of GET /api/settings,
    and this route isn't in API_KEY_ALLOWED_PATHS, so the extension's own API key
    can't be used to read -- or rotate -- itself)."""
    return {"extension_api_key": ensure_extension_api_key(session)}


@router.post("/regenerate-extension-key")
def regenerate_extension_key(session: Session = Depends(get_session)):
    key = secrets.token_urlsafe(24)
    set_setting(session, "extension_api_key", key)
    return {"extension_api_key": key}


EXTENSION_DIR = Path(__file__).resolve().parent.parent.parent / "browser-extension"


@router.get("/extension.zip")
def download_extension():
    """The browser extension as a zip, so it can be installed from the server you
    already have open (it holds no secrets: the API key is typed into its popup)."""
    if not EXTENSION_DIR.is_dir():
        raise HTTPException(404, "The browser extension is not bundled with this install")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(EXTENSION_DIR.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                archive.write(path, path.relative_to(EXTENSION_DIR).as_posix())
    return Response(buffer.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="model-hub-extension.zip"'})
