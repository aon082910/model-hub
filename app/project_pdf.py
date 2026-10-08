"""A one-file PDF of a project: description, notes, the models (with pictures,
designer, license, listing link and filament), filament totals, and the full
parts list with what is still to buy. Built from the same data the project page
shows (see routers/projects.py::_project_json)."""
import io
import os
from datetime import datetime
from typing import Optional
from xml.sax.saxutils import escape

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app import sources
from app.config import THUMB_DIR

PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN = 16 * mm
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN

INK = colors.HexColor("#1b1f24")
MUTED = colors.HexColor("#5b6470")
RULE = colors.HexColor("#d5d9df")
ACCENT = colors.HexColor("#2f6fdc")
SHADE = colors.HexColor("#f2f4f7")
WARN = colors.HexColor("#b3261e")
GOOD = colors.HexColor("#1e7a46")

CATEGORY_TITLES = {"electronics": "Electronics", "parts": "Parts & hardware", "supplies": "Supplies"}
_FONT = {"regular": "Helvetica", "bold": "Helvetica-Bold"}
_fonts_ready = False


def _register_fonts() -> None:
    """DejaVu Sans (shipped with matplotlib, already a dependency) so names with
    accents, dashes, degree signs and similar print correctly; plain Helvetica
    only covers basic Latin. Falls back to Helvetica if the files are missing."""
    global _fonts_ready
    if _fonts_ready:
        return
    _fonts_ready = True
    try:
        import matplotlib
        folder = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")
        pdfmetrics.registerFont(TTFont("DejaVuSans", os.path.join(folder, "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", os.path.join(folder, "DejaVuSans-Bold.ttf")))
        pdfmetrics.registerFontFamily("DejaVuSans", normal="DejaVuSans", bold="DejaVuSans-Bold",
                                      italic="DejaVuSans", boldItalic="DejaVuSans-Bold")
        _FONT.update(regular="DejaVuSans", bold="DejaVuSans-Bold")
    except Exception:
        pass


def _styles() -> dict:
    base = getSampleStyleSheet()["Normal"]
    regular, bold = _FONT["regular"], _FONT["bold"]

    def make(name, **kw):
        return ParagraphStyle(name, parent=base, fontName=kw.pop("fontName", regular), textColor=kw.pop("textColor", INK), **kw)

    return {
        "title": make("title", fontName=bold, fontSize=22, leading=26, spaceAfter=2),
        "meta": make("meta", fontSize=9, leading=12, textColor=MUTED),
        "h2": make("h2", fontName=bold, fontSize=13, leading=16, spaceBefore=14, spaceAfter=6),
        "h3": make("h3", fontName=bold, fontSize=10, leading=13, spaceBefore=8, spaceAfter=3, textColor=MUTED),
        "body": make("body", fontSize=9.5, leading=13),
        "small": make("small", fontSize=8, leading=10.5, textColor=MUTED),
        "cell": make("cell", fontSize=8.5, leading=11),
        "cellb": make("cellb", fontName=bold, fontSize=8.5, leading=11),
        "head": make("head", fontName=bold, fontSize=8, leading=10, textColor=MUTED),
        "headr": make("headr", fontName=bold, fontSize=8, leading=10, textColor=MUTED, alignment=TA_RIGHT),
        "right": make("right", fontSize=8.5, leading=11, alignment=TA_RIGHT),
        "rightb": make("rightb", fontName=bold, fontSize=8.5, leading=11, alignment=TA_RIGHT),
        "warn": make("warn", fontSize=8.5, leading=11, textColor=WARN),
        "good": make("good", fontSize=8.5, leading=11, textColor=GOOD),
    }


def _p(text, style) -> Paragraph:
    return Paragraph(escape(str(text if text is not None else "")).replace("\n", "<br/>"), style)


def _link(url: Optional[str], label: str, style) -> Paragraph:
    if url and url.lower().startswith(("http://", "https://")):
        return Paragraph(f'<link href="{escape(url, {chr(34): "&quot;"})}" color="#2f6fdc">{escape(label)}</link>', style)
    return Paragraph("", style)


def _money(value) -> str:
    return "" if value is None else f"${value:,.2f}"


def _model_picture(model: dict, max_w: float, max_h: float) -> Optional[Image]:
    """The model's rendered thumbnail, else its first listing picture, scaled to fit."""
    candidates = []
    if model.get("thumbnail_path"):
        candidates.append(THUMB_DIR / model["thumbnail_path"])
    for name in model.get("source_images") or []:
        path = sources.image_path(model["id"], name)
        if path:
            candidates.append(path)
    for path in candidates:
        try:
            with PILImage.open(path) as img:
                width, height = img.size
            if width <= 0 or height <= 0:
                continue
            scale = min(max_w / width, max_h / height)
            return Image(str(path), width=width * scale, height=height * scale)
        except Exception:
            continue
    return None


def _table(rows, widths, extra=None, header=True) -> Table:
    table = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), SHADE), ("LINEBELOW", (0, 0), (-1, 0), 0.6, RULE)]
    table.setStyle(TableStyle(style + (extra or [])))
    return table


def build_project_pdf(project: dict) -> bytes:
    """project is the dict from routers/projects.py::_project_json."""
    _register_fonts()
    st = _styles()
    story = []

    story.append(_p(project["name"], st["title"]))
    created = project.get("created_at")
    created_text = created.strftime("%Y-%m-%d") if hasattr(created, "strftime") else str(created or "")[:10]
    story.append(_p(f"Status: {project['status']}   |   Created {created_text}   |   Exported {datetime.now():%Y-%m-%d %H:%M}",
                    st["meta"]))
    story.append(Spacer(1, 8))

    cost = project.get("cost_needed") or 0
    summary = [
        [_p("Parts to get", st["head"]), _p("Cost still to buy", st["head"]), _p("3D models", st["head"]),
         _p("Filament planned", st["head"])],
        [_p(f"{project['parts_missing']} of {project['parts_total']}" if project["parts_total"] else "no parts listed", st["cellb"]),
         _p(_money(cost) if cost else "-", st["cellb"]),
         _p(len(project["models"]), st["cellb"]),
         _p(f"{project['filament_grams']:g} g" if project["filament_grams"] else "-", st["cellb"])],
    ]
    story.append(_table(summary, [CONTENT_WIDTH / 4] * 4, [("BACKGROUND", (0, 1), (-1, 1), SHADE)]))

    if project.get("description"):
        story += [Spacer(1, 8), _p(project["description"], st["body"])]
    if project.get("notes"):
        story += [Paragraph("Notes", st["h2"]), _p(project["notes"], st["body"])]

    # ---- models ----
    if project["models"]:
        story.append(Paragraph("3D models", st["h2"]))
        picture_w, picture_h = 34 * mm, 26 * mm
        for model in project["models"]:
            info = [_p(model["filename"], st["cellb"])]
            by = " · ".join(x for x in (f"by {model['designer']}" if model.get("designer") else "", model.get("license") or "") if x)
            if by:
                info.append(_p(by, st["small"]))
            if model.get("source_url"):
                info.append(_link(model["source_url"], model.get("source_title") or model["source_url"], st["small"]))
            for line in model.get("filament") or []:
                left = f" (spool has {line['remaining_g']:g} g)" if line.get("remaining_g") is not None else ""
                info.append(_p(f"{line['filament_label']}: {line['grams']:g} g{left}", st["small"]))
            if not model.get("filament"):
                info.append(_p("No filament assigned", st["small"]))
            picture = _model_picture(model, picture_w, picture_h)
            row = Table([[picture or _p("", st["small"]), info]], colWidths=[picture_w + 6, CONTENT_WIDTH - picture_w - 6])
            row.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
                ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
            ]))
            story.append(KeepTogether(row))

    # ---- filament totals ----
    if project["filament_totals"]:
        story.append(Paragraph("Filament", st["h2"]))
        rows = [[_p("Spool", st["head"]), _p("Planned", st["headr"]), _p("On hand", st["headr"]), _p("", st["head"])]]
        for t in project["filament_totals"]:
            note = _p("not enough", st["warn"]) if t.get("short") else _p("", st["cell"])
            rows.append([_p(t["filament_label"], st["cell"]), _p(f"{t['grams']:g} g", st["right"]),
                         _p("" if t["remaining_g"] is None else f"{t['remaining_g']:g} g", st["right"]), note])
        if project.get("filament_deducted"):
            rows.append([_p("Already subtracted from your filament inventory.", st["small"]), "", "", ""])
        story.append(_table(rows, [CONTENT_WIDTH * 0.5, CONTENT_WIDTH * 0.15, CONTENT_WIDTH * 0.15, CONTENT_WIDTH * 0.2]))

    # ---- cost ----
    cost_info = project.get("cost")
    if cost_info and cost_info["total"] > 0:
        story.append(Paragraph("What it costs", st["h2"]))
        rows = [[_p("", st["head"]), _p("Amount", st["headr"]), _p("", st["head"])]]
        rows.append([_p("Filament", st["cell"]), _p(_money(cost_info["filament"]), st["right"]),
                     _p(f"{cost_info['filament_unpriced_g']:g} g on spools without a price" if cost_info["filament_unpriced_g"] else "", st["small"])])
        rows.append([_p("Parts (all, including ones you already have)", st["cell"]), _p(_money(cost_info["parts"]), st["right"]),
                     _p(f"{cost_info['parts_unpriced']} without a price" if cost_info["parts_unpriced"] else "", st["small"])])
        if cost_info["electricity"] is not None:
            rows.append([_p("Electricity (about %g h printing at %g W)" % (cost_info["print_hours"], cost_info["printer_watts"]), st["cell"]),
                         _p(_money(cost_info["electricity"]), st["right"]), _p("rough", st["small"])])
        rows.append([_p("Total", st["cellb"]), _p(_money(cost_info["total"]), st["rightb"]), _p("", st["cell"])])
        story.append(_table(rows, [CONTENT_WIDTH * 0.55, CONTENT_WIDTH * 0.2, CONTENT_WIDTH * 0.25]))

    # ---- parts ----
    story.append(Paragraph("Parts list", st["h2"]))
    if not project["parts"]:
        story.append(_p("No parts listed.", st["body"]))
    else:
        widths = [CONTENT_WIDTH * f for f in (0.40, 0.07, 0.07, 0.09, 0.11, 0.12, 0.14)]
        rows = [[_p(h, st["head"] if h in ("Part", "Buy") else st["headr"])
                 for h in ("Part", "Need", "Own", "To get", "Unit cost", "Cost to buy", "Buy")]]
        extra = []
        for category in ("electronics", "parts", "supplies"):
            group = [p for p in project["parts"] if p["category"] == category]
            if not group:
                continue
            rows.append([_p(CATEGORY_TITLES[category], st["h3"]), "", "", "", "", "", ""])
            extra += [("SPAN", (0, len(rows) - 1), (-1, len(rows) - 1)), ("BACKGROUND", (0, len(rows) - 1), (-1, len(rows) - 1), SHADE)]
            for part in group:
                cell = [_p(part["name"], st["cell"])]
                if part.get("notes"):
                    cell.append(_p(part["notes"], st["small"]))
                needed = part["quantity_needed"]
                rows.append([
                    cell, _p(part["quantity"], st["right"]), _p(part["quantity_owned"], st["right"]),
                    _p(needed if needed else "have all", st["rightb"] if needed else st["right"]),
                    _p(_money(part.get("unit_cost")), st["right"]), _p(_money(part.get("cost_needed")), st["right"]),
                    _link(part.get("purchase_url"), "link", st["cell"]),
                ])
        rows.append([_p("Estimated cost still to buy", st["cellb"]), "", "", "", "", _p(_money(cost) if cost else "-", st["rightb"]), ""])
        extra += [("LINEABOVE", (0, len(rows) - 1), (-1, len(rows) - 1), 0.8, INK)]
        story.append(_table(rows, widths, extra))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(_FONT["regular"], 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN, 9 * mm, f"{project['name']} - Model Hub project export")
        canvas.drawRightString(PAGE_WIDTH - MARGIN, 9 * mm, f"Page {doc.page}")
        canvas.restoreState()

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN, bottomMargin=16 * mm,
        title=f"{project['name']} - project", author="Model Hub",
    )
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()
