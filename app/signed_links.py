"""Short-lived links to one model's file, so a slicer on your computer can fetch it without your login.

The link names the model and an expiry and carries an HMAC made with this server's secret key, so it cannot be changed to another
model or kept alive longer. It only ever gives that one file, read-only, for a quarter of an hour.
"""
import hashlib
import hmac
import time

TTL_SECONDS = 15 * 60


def _sign(model_id: int, expires: int) -> str:
    from app.auth import _secret_key
    return hmac.new(_secret_key(), f"dl:{model_id}:{expires}".encode(), hashlib.sha256).hexdigest()[:40]


def make(model_id: int, now: float = None) -> tuple:
    expires = int((time.time() if now is None else now) + TTL_SECONDS)
    return expires, _sign(model_id, expires)


def valid(model_id: int, expires, signature: str, now: float = None) -> bool:
    try:
        expires = int(expires)
    except (TypeError, ValueError):
        return False
    if expires < (time.time() if now is None else now):
        return False
    return hmac.compare_digest(_sign(model_id, expires), str(signature))
