"""An invoice as a PDF (reportlab, which Model Hub already uses for its labels and project sheets)."""
import io
import json

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from xml.sax.saxutils import escape

from sqlmodel import Session

from app import invoices
from app.models import Invoice
from app.settings_store import get_setting


def _p(text, style):
    return Paragraph(escape(str(text if text is not None else "")).replace("\n", "<br/>"), style)


def render(session: Session, invoice: Invoice) -> bytes:
    data = invoices.as_json(session, invoice)
    cur = (get_setting(session, "quote_currency", "") or "")[:6]
    shop = (get_setting(session, "shop_name", "") or "").strip() or "Invoice"
    address = (get_setting(session, "shop_address", "") or "").strip()
    footer = (get_setting(session, "invoice_footer", "") or "").strip()
    money = lambda v: f"{cur}{float(v):,.2f}"
    base = getSampleStyleSheet()
    normal = ParagraphStyle("n", parent=base["Normal"], fontSize=10, leading=13)
    small = ParagraphStyle("s", parent=normal, fontSize=8.5, textColor=colors.HexColor("#555555"))
    right = ParagraphStyle("r", parent=normal, alignment=TA_RIGHT)
    title = ParagraphStyle("t", parent=base["Title"], fontSize=22, alignment=0, spaceAfter=2)
    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm, title=f"Invoice {data['number']}")
    story = [_p(shop, title), _p(address, small), Spacer(1, 6 * mm)]
    head = Table([[_p(f"INVOICE {data['number']}", ParagraphStyle("h", parent=normal, fontSize=14, leading=17)), _p(f"Issued {data['issued_on']}\nDue {data['due_on']} ({data['terms_label']})", right)],
                  [_p("Bill to", small), ""], [_p(data["customer"] + ("\n" + data["contact"] if data["contact"] else ""), normal), ""]], colWidths=[100 * mm, 74 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [head, Spacer(1, 6 * mm)]
    rows = [[_p("Item", normal), _p("Qty", right), _p("Each", right), _p("Discount", right), _p("Total", right)]]
    for line in data["lines"]:
        rows.append([_p(line["name"], normal), _p(line["quantity"], right), _p(money(line["unit"]), right), _p(f"{line['discount_pct']:g}%" if line.get("discount_pct") else "", right), _p(money(line["line"]), right)])
    table = Table(rows, colWidths=[78 * mm, 16 * mm, 28 * mm, 22 * mm, 30 * mm], repeatRows=1)
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")), ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [table, Spacer(1, 4 * mm)]
    sums = [["Subtotal", money(data["subtotal"])]]
    if data["customer_discount"]:
        sums.append([f"Customer discount ({data['customer_discount_pct']:g}%)", "-" + money(data["customer_discount"])])
    if data["tax"] or data["tax_rate_pct"]:
        sums.append([f"Tax ({data['tax_rate_pct']:g}%)", money(data["tax"])])
    sums.append(["Total", money(data["total"])])
    if data["paid"]:
        sums.append(["Paid", "-" + money(data["paid"])])
        sums.append(["Balance due", money(data["balance"])])
    totals = Table([[_p(a, right), _p(b, right)] for a, b in sums], colWidths=[120 * mm, 54 * mm])
    totals.setStyle(TableStyle([("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold")]))
    story += [totals]
    if data["status"] == "paid":
        story += [Spacer(1, 4 * mm), _p("PAID. Thank you.", normal)]
    elif data["status"] == "void":
        story += [Spacer(1, 4 * mm), _p("VOID", normal)]
    if data["notes"]:
        story += [Spacer(1, 6 * mm), _p(data["notes"], normal)]
    if footer:
        story += [Spacer(1, 10 * mm), _p(footer, small)]
    doc.build(story)
    return out.getvalue()
