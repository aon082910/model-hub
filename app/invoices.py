"""Invoices for orders: made from an order's lines, with a customer's payment terms, a standing discount, a credit limit, tax, payments and a PDF.

An invoice is a *snapshot*: the lines and money as they were when it was made, so changing a price later does not alter what was sent. One live (not void) invoice per order. A payment can be
recorded in parts; the invoice is *paid* when the payments cover it, which also marks the order paid. A customer's **credit limit** is the most they may owe in unpaid invoices at once: a new
invoice that would go over it is refused unless you say to go on. This is simple bookkeeping for a small print shop (no ledger, no tax filing): keep your accounts where they belong."""
import json
import math
import re
from datetime import date, datetime, timedelta
from typing import Optional

from sqlmodel import Session, select

from app.models import CustomerTerms, Invoice, Order, OrderItem, Payment
from app.settings_store import get_setting

TERMS = {"due_on_receipt": 0, "net_15": 15, "net_30": 30, "net_60": 60}
TERMS_LABELS = {"due_on_receipt": "Due on receipt", "net_15": "Net 15 days", "net_30": "Net 30 days", "net_60": "Net 60 days"}
CENT = 0.005


class InvoiceError(ValueError):
    """Something to tell the person; the message is safe to show."""


def name_key(customer: str) -> str:
    return re.sub(r"\s+", " ", (customer or "").strip().lower())


def money(value) -> float:
    return round(float(value or 0) + 0.0, 2)


def _number_setting(session: Session, key: str, default: float = 0.0) -> float:
    try:
        return float(get_setting(session, key, "") or default)
    except ValueError:
        return default


def terms_for(session: Session, customer: str) -> Optional[CustomerTerms]:
    return session.exec(select(CustomerTerms).where(CustomerTerms.name_key == name_key(customer))).first()


def paid_amount(session: Session, invoice_id: int) -> float:
    return money(sum(p.amount for p in session.exec(select(Payment).where(Payment.invoice_id == invoice_id)).all()))


def balance(session: Session, invoice: Invoice) -> float:
    return 0.0 if invoice.status == "void" else money(invoice.total - paid_amount(session, invoice.id))


def outstanding(session: Session, customer: str) -> float:
    """What a customer owes now: the unpaid part of their live invoices."""
    key = name_key(customer)
    return money(sum(balance(session, i) for i in session.exec(select(Invoice).where(Invoice.status == "issued")).all() if name_key(i.customer) == key))


def next_number(session: Session) -> str:
    prefix = (get_setting(session, "invoice_prefix", "") or "INV-").strip()[:12] or "INV-"
    top = 0
    for number in session.exec(select(Invoice.number)).all():
        m = re.search(r"(\d+)$", number or "")
        if m:
            top = max(top, int(m.group(1)))
    return f"{prefix}{top + 1:04d}"


def line_amount(unit: float, quantity: int, discount_pct: Optional[float]) -> float:
    return money(unit * quantity * (1 - (discount_pct or 0) / 100))


def create(session: Session, order: Order, terms: Optional[str] = None, notes: Optional[str] = None, force: bool = False, today: Optional[date] = None) -> Invoice:
    """An invoice from the order as it is now. Raises InvoiceError (nothing priced, a live invoice already exists, or the credit limit would be passed)."""
    from app.routers.orders import _order_json
    if order.status in ("cancelled",):
        raise InvoiceError("A cancelled order cannot be invoiced")
    if session.exec(select(Invoice).where(Invoice.order_id == order.id, Invoice.status != "void")).first():
        raise InvoiceError("This order already has an invoice (void it first to make another)")
    detail = _order_json(session, order)
    lines, unpriced = [], []
    for item in detail["items"]:
        used = item["price_used"]
        if used is None:
            unpriced.append(item["filename"] or f"model {item['model_id']}")
            continue
        row = session.get(OrderItem, item["id"])
        lines.append({"name": re.sub(r"[_-]+", " ", (item["filename"] or "An item").rsplit(".", 1)[0]).strip()[:120], "quantity": item["quantity"], "unit": money(used),
                      "discount_pct": row.discount_pct if row and row.discount_pct else None, "line": line_amount(used, item["quantity"], row.discount_pct if row else None)})
    if unpriced:
        raise InvoiceError("Give a price to: " + ", ".join(unpriced[:5]) + (" and more" if len(unpriced) > 5 else ""))
    if not lines:
        raise InvoiceError("The order has nothing to invoice")
    customer_terms = terms_for(session, order.customer)
    chosen = terms or (customer_terms.payment_terms if customer_terms else "due_on_receipt")
    if chosen not in TERMS:
        raise InvoiceError("The payment terms must be one of: " + ", ".join(TERMS))
    subtotal = money(sum(l["line"] for l in lines))
    pct = customer_terms.discount_pct if customer_terms and customer_terms.discount_pct else None
    discount = money(subtotal * pct / 100) if pct else 0.0
    rate = _number_setting(session, "tax_rate_pct")
    tax = money((subtotal - discount) * rate / 100)
    total = money(subtotal - discount + tax)
    limit = customer_terms.credit_limit if customer_terms else None
    if limit is not None and not force and outstanding(session, order.customer) + total > limit + CENT:
        raise InvoiceError(f"Credit limit: {order.customer} owes {outstanding(session, order.customer):.2f} already and may owe at most {limit:.2f}; this invoice is {total:.2f}")
    today = today or date.today()
    invoice = Invoice(order_id=order.id, number=next_number(session), customer=order.customer, contact=order.contact, issued_on=today.isoformat(),
                      due_on=(today + timedelta(days=TERMS[chosen])).isoformat(), terms=chosen, lines_json=json.dumps(lines), subtotal=subtotal,
                      customer_discount_pct=pct, customer_discount=discount, tax_rate_pct=rate, tax=tax, total=total, notes=(notes or "").strip()[:500] or None)
    session.add(invoice)
    session.commit()
    session.refresh(invoice)
    return invoice


def add_payment(session: Session, invoice: Invoice, amount, method: Optional[str], paid_on: Optional[str], note: Optional[str]) -> Payment:
    if invoice.status == "void":
        raise InvoiceError("A void invoice takes no payments")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0 or amount > 10_000_000:
        raise InvoiceError("The amount must be a positive number")
    left = balance(session, invoice)
    if amount > left + CENT:
        raise InvoiceError(f"Only {left:.2f} is still owed")
    day = paid_on or date.today().isoformat()
    try:
        date.fromisoformat(day)
    except (TypeError, ValueError):
        raise InvoiceError("The date must look like 2026-10-31")
    payment = Payment(invoice_id=invoice.id, amount=money(amount), method=(method or "").strip()[:40] or None, paid_on=day, note=(note or "").strip()[:200] or None)
    session.add(payment)
    session.flush()
    settle(session, invoice)
    session.commit()
    session.refresh(payment)
    return payment


def settle(session: Session, invoice: Invoice) -> None:
    """Mark the invoice (and its order) paid when the payments cover it, and not paid when they stop doing so."""
    if invoice.status == "void":
        return
    paid = paid_amount(session, invoice.id) >= invoice.total - CENT and invoice.total > 0
    invoice.status = "paid" if paid else "issued"
    session.add(invoice)
    order = session.get(Order, invoice.order_id)
    if order:
        order.paid = paid
        session.add(order)


def void(session: Session, invoice: Invoice) -> None:
    invoice.status = "void"
    session.add(invoice)
    order = session.get(Order, invoice.order_id)
    if order:
        order.paid = False
        session.add(order)
    session.commit()


def as_json(session: Session, invoice: Invoice) -> dict:
    paid = paid_amount(session, invoice.id)
    left = balance(session, invoice)
    return {"id": invoice.id, "order_id": invoice.order_id, "number": invoice.number, "customer": invoice.customer, "contact": invoice.contact, "issued_on": invoice.issued_on, "due_on": invoice.due_on,
            "terms": invoice.terms, "terms_label": TERMS_LABELS.get(invoice.terms, invoice.terms), "lines": json.loads(invoice.lines_json or "[]"), "subtotal": invoice.subtotal,
            "customer_discount_pct": invoice.customer_discount_pct, "customer_discount": invoice.customer_discount, "tax_rate_pct": invoice.tax_rate_pct, "tax": invoice.tax, "total": invoice.total,
            "paid": paid, "balance": left, "status": invoice.status, "notes": invoice.notes,
            "overdue": invoice.status == "issued" and left > CENT and invoice.due_on < date.today().isoformat(),
            "payments": [{"id": p.id, "amount": p.amount, "method": p.method, "paid_on": p.paid_on, "note": p.note}
                         for p in session.exec(select(Payment).where(Payment.invoice_id == invoice.id).order_by(Payment.paid_on, Payment.id)).all()]}


def check_overdue(session: Session, today: Optional[date] = None) -> list:
    """Tell once about each invoice that is past its due date and not paid. Returns their numbers."""
    from app.notify import notify_event
    day = (today or date.today()).isoformat()
    told = []
    for invoice in session.exec(select(Invoice).where(Invoice.status == "issued")).all():
        if invoice.due_on < day and not invoice.overdue_told and balance(session, invoice) > CENT:
            invoice.overdue_told = True
            session.add(invoice)
            session.commit()
            notify_event(session, "invoice_overdue", f"Model Hub: invoice {invoice.number} is overdue",
                         f"{invoice.customer} owes {balance(session, invoice):.2f} on invoice {invoice.number}, which was due on {invoice.due_on}.")
            told.append(invoice.number)
    return told


def scheduled(session: Session) -> list:
    return check_overdue(session)
