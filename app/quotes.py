"""A page for a customer to look at a quote and say yes or no, without signing in.

An order (while it is still a *quote*) can have a link with a secret in it. The page shows what the customer needs: the items with their quantities and prices, the total and the date
wanted, and two buttons. It never shows what the prints cost you, your profit, your notes, the customer's contact details or anything else of Model Hub. An answer changes the order
(yes: *accepted*, no: *cancelled*) once, and you are told. Links can be rotated or removed at any time, and stop working when the order moves on from *quote*."""
import hmac
import re
import secrets
import time
from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from app import activity
from app.models import Order
from app.notify import notify_event
from app.settings_store import get_setting

_misses: dict = {}                      # client -> [times of unknown-link lookups]
MISS_LIMIT, MISS_WINDOW = 30, 600


def too_many_misses(client: str) -> bool:
    now = time.time()
    recent = [t for t in _misses.get(client, []) if now - t < MISS_WINDOW]
    _misses[client] = recent
    return len(recent) >= MISS_LIMIT


def note_miss(client: str) -> None:
    _misses.setdefault(client, []).append(time.time())


def make_link(session: Session, order: Order, rotate: bool = False) -> str:
    if not order.public_token or rotate:
        order.public_token = secrets.token_urlsafe(24)
        session.add(order)
        session.commit()
    return order.public_token


def remove_link(session: Session, order: Order) -> None:
    order.public_token = None
    session.add(order)
    session.commit()


def find(session: Session, given: str) -> Optional[Order]:
    if not given or len(given) > 80:
        return None
    order = session.exec(select(Order).where(Order.public_token == given)).first()
    return order if order and order.public_token and hmac.compare_digest(order.public_token.encode(), given.encode()) else None


def snapshot(session: Session, order: Order) -> dict:
    from app.routers.orders import _order_json
    detail = _order_json(session, order)
    items = []
    for i in detail["items"]:
        name = re.sub(r"[_-]+", " ", (i["filename"] or "An item").rsplit(".", 1)[0]).strip()
        items.append({"name": name[:120], "quantity": i["quantity"], "unit_price": i["price_used"], "line_price": i["line_price"]})
    priced = [i["line_price"] for i in items]
    return {"shop": (get_setting(session, "shop_name", "") or "")[:80], "currency": (get_setting(session, "quote_currency", "") or "")[:6], "order": order.id, "customer": order.customer,
            "status": order.status, "due_date": order.due_date, "items": items, "total": round(sum(p for p in priced if p is not None), 2) if any(p is not None for p in priced) else None,
            "unpriced": any(p is None for p in priced), "can_answer": order.status == "quote"}


def answer(session: Session, order: Order, accept: bool, name: str, message: str) -> str:
    """The customer's answer. Only a quote can be answered, and only once."""
    if order.status != "quote":
        raise ValueError("This quote has already been answered or has moved on")
    name = re.sub(r"[\r\n\t]+", " ", name or "").strip()[:80]
    message = re.sub(r"[\r\n\t]+", " ", message or "").strip()[:300]
    order.status = "accepted" if accept else "cancelled"
    stamp = f"The customer {'accepted' if accept else 'declined'} this quote online on {datetime.utcnow():%Y-%m-%d %H:%M} UTC" + (f" (signed: {name})" if name else "") + (f". They wrote: {message}" if message else "")
    order.notes = ((order.notes + "\n") if order.notes else "") + stamp
    session.add(order)
    session.commit()
    activity.record(session, "the customer", "order", f"Quote for {order.customer} {'accepted' if accept else 'declined'} online")
    notify_event(session, "quote_answer", f"Model Hub: {order.customer} {'accepted' if accept else 'declined'} a quote", message or ("Accepted" if accept else "Declined"))
    return order.status


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex"><title>Your quote</title>
<style>
:root{color-scheme:light dark;--bg:#f6f7f9;--card:#fff;--text:#1c2330;--muted:#667085;--line:#e3e6ec;--accent:#2563eb;--ok:#15803d;--bad:#b42318}
@media (prefers-color-scheme:dark){:root{--bg:#10141c;--card:#18202c;--text:#e8ecf3;--muted:#9aa5b6;--line:#2a3445;--accent:#6ea0ff;--ok:#4ade80;--bad:#f87171}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,sans-serif}
main{max-width:640px;margin:0 auto;padding:24px 16px}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px}
h1{font-size:1.4rem;margin:0 0 4px}.muted{color:var(--muted)}table{width:100%;border-collapse:collapse;margin:16px 0}th,td{padding:8px 4px;border-bottom:1px solid var(--line);text-align:left}
td.n,th.n{text-align:right;white-space:nowrap}.total td{font-weight:700;border-bottom:0}input,textarea{width:100%;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text);font:inherit;margin:4px 0 10px}
button{font:inherit;padding:10px 18px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--text);cursor:pointer;margin-right:8px}button.yes{background:var(--accent);border-color:var(--accent);color:#fff}
.msg{margin-top:14px;font-weight:600}.ok{color:var(--ok)}.bad{color:var(--bad)}
</style></head><body><main><div class="card" id="box"><p class="muted">Loading...</p></div></main>
<script>
const base = location.pathname.replace(/\\/$/, '');
const box = document.getElementById('box');
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function load() {
  const r = await fetch(base + '/data');
  if (!r.ok) { box.innerHTML = '<h1>This link does not work</h1><p class="muted">It may have been replaced or switched off. Ask for a new one.</p>'; return; }
  const q = await r.json(), cur = q.currency || '', money = v => v == null ? 'to be agreed' : cur + v.toFixed(2);
  const word = {quote: 'Waiting for your answer', accepted: 'Accepted', printing: 'Being printed', ready: 'Ready', delivered: 'Delivered', cancelled: 'Declined or cancelled'}[q.status] || q.status;
  box.innerHTML = '<h1>' + esc(q.shop || 'Your quote') + '</h1><div class="muted">For ' + esc(q.customer) + (q.due_date ? ' &middot; wanted by ' + esc(q.due_date) : '') + ' &middot; ' + esc(word) + '</div>' +
    '<table><thead><tr><th>Item</th><th class="n">Qty</th><th class="n">Each</th><th class="n">Total</th></tr></thead><tbody>' +
    q.items.map(i => '<tr><td>' + esc(i.name) + '</td><td class="n">' + i.quantity + '</td><td class="n">' + money(i.unit_price) + '</td><td class="n">' + money(i.line_price) + '</td></tr>').join('') +
    '<tr class="total"><td colspan="3">Total' + (q.unpriced ? ' (without the items still to be agreed)' : '') + '</td><td class="n">' + (q.total == null ? '' : money(q.total)) + '</td></tr></tbody></table>' +
    (q.can_answer ? '<label>Your name (optional)<input id="name" maxlength="80" autocomplete="name"></label><label>A message (optional)<textarea id="message" rows="2" maxlength="300"></textarea></label>' +
      '<button class="yes" id="yes">Accept this quote</button><button id="no">Decline</button><div class="msg" id="msg"></div>' : '');
  if (!q.can_answer) return;
  for (const [id, accept] of [['yes', true], ['no', false]]) document.getElementById(id).onclick = async () => {
    const out = document.getElementById('msg');
    if (!accept && !confirm('Decline this quote?')) return;
    const r = await fetch(base + '/respond', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({accept, name: document.getElementById('name').value, message: document.getElementById('message').value})});
    if (r.ok) { out.className = 'msg ok'; out.textContent = accept ? 'Thank you, the quote is accepted.' : 'The quote is declined.'; document.getElementById('yes').remove(); document.getElementById('no').remove(); }
    else { out.className = 'msg bad'; out.textContent = (await r.json().catch(() => ({}))).detail || 'That did not work'; }
  };
}
load();
</script></body></html>"""
