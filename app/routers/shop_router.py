import hmac

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session

from app import shop_orders
from app.db import get_session
from app.settings_store import get_setting

router = APIRouter(prefix="/api/settings/shops", tags=["shops"])          # administrator only (see app.auth)
public_router = APIRouter(tags=["shops"], include_in_schema=False)


def _info(session: Session, request: Request) -> dict:
    base = str(request.base_url).rstrip("/")
    token = get_setting(session, "shop_hook_token", "")
    return {"woocommerce": shop_orders.woocommerce_configured(session), "shipstation": shop_orders.shipstation_configured(session),
            "sync_on": get_setting(session, "shop_sync", "") == "true", "last_sync": get_setting(session, "shop_sync_last", "") or None,
            "hook_url": f"{base}/hooks/shop/{token}" if token else None, "hook_secret_set": bool(get_setting(session, "shop_hook_secret", ""))}


@router.get("")
def get_shops(request: Request, session: Session = Depends(get_session)):
    return _info(session, request)


@router.post("/sync")
def sync_now(request: Request, session: Session = Depends(get_session)):
    """Ask the configured shops for their waiting orders now."""
    if not (shop_orders.woocommerce_configured(session) or shop_orders.shipstation_configured(session)):
        raise HTTPException(400, "Fill in WooCommerce or ShipStation first")
    return {"results": shop_orders.sync(session)}


@router.post("/hook-address")
def make_hook_address(payload: dict, request: Request, session: Session = Depends(get_session)):
    """A web hook address for Shopify or WooCommerce to call (rotate: true makes a new one; the old one stops working). It works only with the shared secret set as well."""
    if payload.get("rotate") is True:
        shop_orders.rotate_hook_token(session)
    else:
        shop_orders.hook_token(session)
    return _info(session, request)


@public_router.post("/hooks/shop/{given}")
async def shop_hook(given: str, request: Request, session: Session = Depends(get_session)):
    real = get_setting(session, "shop_hook_token", "")
    if not real or not hmac.compare_digest(real.encode(), given.encode()):
        raise HTTPException(404, "Not found")
    body = await request.body()
    try:
        report = shop_orders.receive(session, body, request.headers.get("x-shopify-hmac-sha256"), request.headers.get("x-wc-webhook-signature"))
    except ValueError:
        raise HTTPException(401, "Not accepted")
    return JSONResponse({"made": len(report["orders"]), "skipped": len(report["skipped"])})
