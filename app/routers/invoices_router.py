"""Invoices, payments and customer terms (see app.invoices)."""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlmodel import Session, select

from app import activity, invoice_pdf, invoices
from app.db import get_session
from app.models import CustomerTerms, Invoice, Order, Payment

router = APIRouter(prefix="/api/invoices", tags=["invoices"])
customers = APIRouter(prefix="/api/customers", tags=["invoices"])


def _get(session: Session, invoice_id: int) -> Invoice:
    row = session.get(Invoice, invoice_id)
    if not row:
        raise HTTPException(404, "Not found")
    return row


@router.get("")
def list_invoices(status: Optional[str] = None, customer: Optional[str] = None, session: Session = Depends(get_session)):
    stmt = select(Invoice).order_by(Invoice.id.desc())
    if status:
        if status not in ("issued", "paid", "void"):
            raise HTTPException(400, "status must be issued, paid or void")
        stmt = stmt.where(Invoice.status == status)
    rows = session.exec(stmt).all()
    if customer:
        key = invoices.name_key(customer)
        rows = [r for r in rows if invoices.name_key(r.customer) == key]
    out = [invoices.as_json(session, r) for r in rows[:500]]
    return {"invoices": out, "owed": invoices.money(sum(i["balance"] for i in out)), "overdue": sum(1 for i in out if i["overdue"])}


@router.post("/from-order/{order_id}")
def make_invoice(order_id: int, payload: dict, request: Request, session: Session = Depends(get_session)):
    """Make the invoice for an order (terms: due_on_receipt, net_15, net_30 or net_60, else the customer's own; force: true goes over a credit limit)."""
    order = session.get(Order, order_id)
    if not order:
        raise HTTPException(404, "Not found")
    terms, notes = payload.get("terms"), payload.get("notes")
    if (terms is not None and not isinstance(terms, str)) or (notes is not None and not isinstance(notes, str)):
        raise HTTPException(400, "terms and notes must be text")
    try:
        invoice = invoices.create(session, order, terms or None, notes, force=payload.get("force") is True)
    except invoices.InvoiceError as e:
        raise HTTPException(409 if "Credit limit" in str(e) or "already has" in str(e) else 400, str(e))
    activity.record(session, activity.actor_of(request), "order", f"Made invoice {invoice.number} for {invoice.customer}")
    return invoices.as_json(session, invoice)


@router.get("/{invoice_id}")
def get_invoice(invoice_id: int, session: Session = Depends(get_session)):
    return invoices.as_json(session, _get(session, invoice_id))


@router.get("/{invoice_id}/pdf")
def invoice_as_pdf(invoice_id: int, session: Session = Depends(get_session)):
    invoice = _get(session, invoice_id)
    return Response(invoice_pdf.render(session, invoice), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{invoice.number}.pdf"'})


@router.post("/{invoice_id}/payments")
def record_payment(invoice_id: int, payload: dict, request: Request, session: Session = Depends(get_session)):
    invoice = _get(session, invoice_id)
    try:
        invoices.add_payment(session, invoice, payload.get("amount"), payload.get("method"), payload.get("paid_on"), payload.get("note"))
    except invoices.InvoiceError as e:
        raise HTTPException(400, str(e))
    activity.record(session, activity.actor_of(request), "order", f"Recorded a payment of {payload.get('amount')} on invoice {invoice.number}")
    session.refresh(invoice)
    return invoices.as_json(session, invoice)


@router.delete("/{invoice_id}/payments/{payment_id}")
def remove_payment(invoice_id: int, payment_id: int, request: Request, session: Session = Depends(get_session)):
    invoice = _get(session, invoice_id)
    payment = session.get(Payment, payment_id)
    if not payment or payment.invoice_id != invoice_id:
        raise HTTPException(404, "Not found")
    session.delete(payment)
    session.flush()
    invoices.settle(session, invoice)
    session.commit()
    activity.record(session, activity.actor_of(request), "order", f"Removed a payment from invoice {invoice.number}")
    session.refresh(invoice)
    return invoices.as_json(session, invoice)


@router.post("/{invoice_id}/void")
def void_invoice(invoice_id: int, request: Request, session: Session = Depends(get_session)):
    if (getattr(request.state, "user", None) or {}).get("role") != "admin":
        raise HTTPException(403, "Only the administrator can void an invoice.")
    invoice = _get(session, invoice_id)
    invoices.void(session, invoice)
    activity.record(session, activity.actor_of(request), "order", f"Voided invoice {invoice.number}")
    session.refresh(invoice)
    return invoices.as_json(session, invoice)


# ---------------------------------------------------------------- customers' terms
def _terms_json(session: Session, row: Optional[CustomerTerms], name: str) -> dict:
    return {"id": row.id if row else None, "name": row.name if row else name, "payment_terms": row.payment_terms if row else "due_on_receipt",
            "credit_limit": row.credit_limit if row else None, "discount_pct": row.discount_pct if row else None, "notes": row.notes if row else None,
            "owes": invoices.outstanding(session, name)}


@customers.get("")
def list_customers(session: Session = Depends(get_session)):
    """Every customer named on an order or given terms, with what they have agreed and what they owe."""
    seen = {}
    for name in session.exec(select(Order.customer)).all():
        seen.setdefault(invoices.name_key(name), name)
    terms = {t.name_key: t for t in session.exec(select(CustomerTerms)).all()}
    for key, t in terms.items():
        seen.setdefault(key, t.name)
    return {"customers": [_terms_json(session, terms.get(key), name) for key, name in sorted(seen.items(), key=lambda kv: kv[1].lower())][:1000], "terms": invoices.TERMS_LABELS}


@customers.put("")
def set_customer_terms(payload: dict, request: Request, session: Session = Depends(get_session)):
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise HTTPException(400, "Give the customer's name as it is on their orders")
    terms = payload.get("payment_terms", "due_on_receipt")
    if terms not in invoices.TERMS:
        raise HTTPException(400, "payment_terms must be one of: " + ", ".join(invoices.TERMS))
    out = {}
    for key, high in (("credit_limit", 100_000_000), ("discount_pct", 100)):
        value = payload.get(key)
        if value in (None, ""):
            out[key] = None
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= high:
            raise HTTPException(400, f"{key} must be a number from 0 to {high}")
        else:
            out[key] = round(float(value), 2)
    notes = payload.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise HTTPException(400, "notes must be text")
    row = invoices.terms_for(session, name) or CustomerTerms(name_key=invoices.name_key(name), name=name.strip())
    row.name, row.payment_terms, row.credit_limit, row.discount_pct, row.notes = name.strip(), terms, out["credit_limit"], out["discount_pct"], (notes or "").strip()[:300] or None
    session.add(row)
    session.commit()
    session.refresh(row)
    activity.record(session, activity.actor_of(request), "order", f"Set the terms of {row.name}")
    return _terms_json(session, row, row.name)
