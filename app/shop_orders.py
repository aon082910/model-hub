"""Orders from a shop, without a spreadsheet: WooCommerce and ShipStation are asked now and then, and Shopify or WooCommerce can *tell* Model Hub (a signed web hook).

Every order, however it arrives, goes through the same steps as a CSV import: one Model Hub order per shop order (it is never made twice: the shop's order number is kept in the notes),
its lines matched to library models by SKU or title, lines that match nothing kept in the notes. New orders start as *accepted* (the customer already paid in the shop).

**What is not here:** Etsy, eBay and TikTok Shop. Their order interfaces need an approved application and an OAuth sign-in with the shop owner's account, which cannot be done or tested from a
self-hosted tool without registering a developer app; for those, export the orders as a spreadsheet and use the CSV import. Shop passwords are never asked for, and the keys you give are only
ever sent to the shop's own address, kept out of backups and shown as dots."""
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import socket
from typing import Optional
from urllib.parse import urlparse

import httpx
from sqlmodel import Session, select

from app import activity
from app.models import Model3D, Order, OrderItem
from app.notify import notify_event
from app.settings_store import get_setting, set_setting

logger = logging.getLogger("modelhub.shop")
SHIPSTATION_API = "https://ssapi.shipstation.com"
MAX_BODY = 1024 * 1024
MAX_PER_SYNC = 100


def _s(session: Session, key: str) -> str:
    return (get_setting(session, key, "") or "").strip()


# ---------------------------------------------------------------- the shared step: shop orders into Model Hub orders
def create_orders(session: Session, groups: dict, dry: bool = False, origin: str = "a shop export") -> dict:
    """groups: {order number: {"customer", "contact", "lines": [{"item", "sku", "quantity", "price"}]}}. Returns the same report as the CSV import."""
    from app.routers import orders as orders_router
    models = session.exec(select(Model3D)).all()
    existing = " ".join(n for n in session.exec(select(Order.notes).where(Order.notes.is_not(None))).all() if n)
    report = {"orders": [], "skipped": [], "lines": 0, "matched": 0, "dry": dry}
    for key, g in groups.items():
        marker = f"[import:{key}]"
        if marker in existing:
            report["skipped"].append(key)
            continue
        matched, missing = [], []
        for line in g["lines"]:
            model = orders_router._match_model(models, line["sku"], line["item"])
            if model:
                matched.append((model, line))
            else:
                missing.append(f"{line['item'] or line['sku'] or 'an item'} x {line['quantity']}")
        report["lines"] += len(g["lines"])
        report["matched"] += len(matched)
        report["orders"].append({"order": key, "customer": g["customer"] or f"Order {key}", "matched": [{"filename": m.filename, "quantity": l["quantity"], "unit_price": l["price"]} for m, l in matched], "unmatched": missing})
        if dry:
            continue
        notes = f"Imported from {origin} {marker}" + (f". Not matched to a model: {'; '.join(missing)}" if missing else "")
        order = Order(customer=(g["customer"] or f"Order {key}")[:120], contact=(g["contact"] or "")[:200] or None, status="accepted", notes=notes[:4000])
        session.add(order)
        session.commit()
        session.refresh(order)
        for model, line in matched[:100]:
            session.add(OrderItem(order_id=order.id, model_id=model.id, quantity=line["quantity"], unit_price=line["price"]))
        session.commit()
        existing += " " + marker
    return report


def _line(item: str, sku: str, quantity, price) -> dict:
    from app.routers.orders import _money
    try:
        qty = max(1, min(10000, int(float(quantity))))
    except (TypeError, ValueError):
        qty = 1
    return {"item": str(item or "")[:200], "sku": str(sku or "")[:80], "quantity": qty, "price": _money(price)}


def from_woocommerce(order: dict) -> Optional[tuple]:
    """(key, group) from a WooCommerce order (the REST body, which a WooCommerce web hook also sends)."""
    if not isinstance(order, dict) or not order.get("id"):
        return None
    billing = order.get("billing") if isinstance(order.get("billing"), dict) else {}
    shipping = order.get("shipping") if isinstance(order.get("shipping"), dict) else {}
    name = " ".join(x for x in (shipping.get("first_name"), shipping.get("last_name")) if x).strip() or " ".join(x for x in (billing.get("first_name"), billing.get("last_name")) if x).strip()
    lines = [_line(i.get("name"), i.get("sku"), i.get("quantity"), i.get("price")) for i in (order.get("line_items") or [])[:200] if isinstance(i, dict)]
    return f"woo-{order.get('number') or order['id']}", {"customer": name, "contact": billing.get("email") or "", "lines": lines}


def from_shopify(order: dict) -> Optional[tuple]:
    if not isinstance(order, dict) or not order.get("id"):
        return None
    customer = order.get("customer") if isinstance(order.get("customer"), dict) else {}
    ship = order.get("shipping_address") if isinstance(order.get("shipping_address"), dict) else {}
    name = (ship.get("name") or " ".join(x for x in (customer.get("first_name"), customer.get("last_name")) if x)).strip()
    lines = [_line(i.get("title") or i.get("name"), i.get("sku"), i.get("quantity"), i.get("price")) for i in (order.get("line_items") or [])[:200] if isinstance(i, dict)]
    return f"shopify-{str(order.get('name') or order['id']).lstrip('#')}", {"customer": name, "contact": order.get("email") or customer.get("email") or "", "lines": lines}


def from_shipstation(order: dict) -> Optional[tuple]:
    if not isinstance(order, dict) or not (order.get("orderNumber") or order.get("orderId")):
        return None
    ship = order.get("shipTo") if isinstance(order.get("shipTo"), dict) else {}
    bill = order.get("billTo") if isinstance(order.get("billTo"), dict) else {}
    lines = [_line(i.get("name"), i.get("sku"), i.get("quantity"), i.get("unitPrice")) for i in (order.get("items") or [])[:200] if isinstance(i, dict) and not i.get("adjustment")]
    return f"shipstation-{order.get('orderNumber') or order.get('orderId')}", {"customer": (ship.get("name") or bill.get("name") or "").strip(), "contact": order.get("customerEmail") or "", "lines": lines}


def ingest(session: Session, pairs: list, origin: str) -> dict:
    """Make Model Hub orders from converted shop orders and tell about new ones."""
    groups = {k: g for k, g in (p for p in pairs if p)}
    report = create_orders(session, groups, False, origin)
    for entry in report["orders"]:
        notify_event(session, "order_new", f"Model Hub: a new order from {origin}", f"{entry['customer']}: {len(entry['matched'])} item(s) matched" + (f", {len(entry['unmatched'])} not matched" if entry["unmatched"] else ""))
    if report["orders"]:
        activity.record(session, origin, "order", f"{len(report['orders'])} order(s) came in from {origin}")
    return report


# ---------------------------------------------------------------- asking the shop
def _safe_shop_url(url: str) -> Optional[str]:
    """The shop's address without a user name, a path trick or a fragment; plain http only for a private network address (a shop on your own network)."""
    try:
        p = urlparse(url)
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password or p.fragment or p.query:
        return None
    if p.scheme == "http":
        try:
            ip = ipaddress.ip_address(socket.gethostbyname(p.hostname))
        except (OSError, ValueError):
            return None
        if not (ip.is_private or ip.is_loopback):
            return None                   # keys must not travel in clear over the internet
    return url.rstrip("/")


def woocommerce_configured(session: Session) -> bool:
    return bool(_safe_shop_url(_s(session, "woo_url")) and _s(session, "woo_key") and _s(session, "woo_secret"))


def shipstation_configured(session: Session) -> bool:
    return bool(_s(session, "shipstation_key") and _s(session, "shipstation_secret"))


def fetch_woocommerce(session: Session) -> list:
    base = _safe_shop_url(_s(session, "woo_url"))
    r = httpx.get(f"{base}/wp-json/wc/v3/orders", params={"status": "processing,on-hold", "per_page": MAX_PER_SYNC, "orderby": "id", "order": "desc"},
                  auth=(_s(session, "woo_key"), _s(session, "woo_secret")), timeout=20, follow_redirects=False)
    if r.status_code in (401, 403):
        raise RuntimeError("WooCommerce did not accept the key (it needs read access)")
    if r.status_code != 200:
        raise RuntimeError(f"WooCommerce answered {r.status_code}")
    body = r.json()
    return [from_woocommerce(o) for o in body] if isinstance(body, list) else []


def fetch_shipstation(session: Session) -> list:
    r = httpx.get(f"{SHIPSTATION_API}/orders", params={"orderStatus": "awaiting_shipment", "pageSize": MAX_PER_SYNC, "sortBy": "OrderDate", "sortDir": "DESC"},
                  auth=(_s(session, "shipstation_key"), _s(session, "shipstation_secret")), timeout=20, follow_redirects=False)
    if r.status_code in (401, 403):
        raise RuntimeError("ShipStation did not accept the key")
    if r.status_code != 200:
        raise RuntimeError(f"ShipStation answered {r.status_code}")
    body = r.json()
    return [from_shipstation(o) for o in (body.get("orders") if isinstance(body, dict) else []) or []]


def sync(session: Session) -> dict:
    """Ask each configured shop for its waiting orders (when "shop_sync" is on or when asked by hand). Returns {shop: report or error}."""
    out = {}
    for name, ready, fetch in (("WooCommerce", woocommerce_configured, fetch_woocommerce), ("ShipStation", shipstation_configured, fetch_shipstation)):
        if not ready(session):
            continue
        try:
            out[name] = ingest(session, fetch(session), name)
        except Exception as e:
            logger.info("%s order sync failed: %s", name, e.__class__.__name__)
            out[name] = {"error": str(e)[:200] if isinstance(e, RuntimeError) else f"Could not reach {name} ({e.__class__.__name__})"}
    set_setting(session, "shop_sync_last", json.dumps({k: ("error" if "error" in v else len(v["orders"])) for k, v in out.items()}))
    return out


def scheduled(session: Session) -> Optional[dict]:
    if get_setting(session, "shop_sync", "") != "true":
        return None
    return sync(session)


# ---------------------------------------------------------------- being told (web hooks)
def hook_token(session: Session) -> str:
    token = get_setting(session, "shop_hook_token", "")
    if not token:
        token = secrets.token_urlsafe(24)
        set_setting(session, "shop_hook_token", token)
    return token


def rotate_hook_token(session: Session) -> str:
    set_setting(session, "shop_hook_token", secrets.token_urlsafe(24))
    return get_setting(session, "shop_hook_token", "")


def hook_enabled(session: Session) -> bool:
    return bool(_s(session, "shop_hook_secret")) and bool(get_setting(session, "shop_hook_token", ""))


def signature_ok(secret: str, body: bytes, shopify: Optional[str], woo: Optional[str]) -> Optional[str]:
    """Which shop signed this body ("shopify" or "woocommerce") with the shared secret, or None. Both sign with HMAC-SHA256, base64."""
    expected = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    if shopify and hmac.compare_digest(expected, shopify.strip()):
        return "shopify"
    if woo and hmac.compare_digest(expected, woo.strip()):
        return "woocommerce"
    return None


def receive(session: Session, body: bytes, shopify_sig: Optional[str], woo_sig: Optional[str]) -> dict:
    """A shop's web hook. Raises ValueError for anything that is not a correctly signed order."""
    secret = _s(session, "shop_hook_secret")
    if not secret or len(body) > MAX_BODY:
        raise ValueError("refused")
    shop = signature_ok(secret, body, shopify_sig, woo_sig)
    if not shop:
        raise ValueError("bad signature")
    try:
        order = json.loads(body)
    except ValueError:
        return {"orders": [], "skipped": [], "lines": 0, "matched": 0, "dry": False}      # WooCommerce's first call is a signed "ping" that is not an order
    pair = (from_shopify if shop == "shopify" else from_woocommerce)(order)
    if not pair or not pair[1]["lines"]:
        return {"orders": [], "skipped": [], "lines": 0, "matched": 0, "dry": False}       # a ping or an order with nothing in it: acknowledged, nothing made
    return ingest(session, [pair], "Shopify" if shop == "shopify" else "WooCommerce")
