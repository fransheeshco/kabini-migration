#!/usr/bin/env python3
"""Generate docs/PROJECT_HANDOFF.pdf from the maintained Markdown source."""

from __future__ import annotations

import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Preformatted,
    Spacer,
    Table,
    TableStyle,
)
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "PROJECT_HANDOFF.md"
OUTPUT = ROOT / "docs" / "PROJECT_HANDOFF.pdf"

INK = colors.HexColor("#1F2933")
BLUE = colors.HexColor("#234E70")
TEAL = colors.HexColor("#2A7F83")
PALE = colors.HexColor("#EAF2F4")
GOLD = colors.HexColor("#C79A45")


def inline(text: str) -> str:
    text = escape(text.strip())
    text = re.sub(r"`([^`]+)`", r'<font name="Courier">\1</font>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"\*([^*]+)\*", r"<i>\1</i>", text)
    return text


def page(canvas, doc) -> None:
    canvas.saveState()
    width, height = A4
    canvas.setStrokeColor(PALE)
    canvas.line(20 * mm, 17 * mm, width - 20 * mm, 17 * mm)
    canvas.setFillColor(colors.HexColor("#637381"))
    canvas.setFont("Helvetica", 8)
    canvas.drawString(20 * mm, 11 * mm, "Treasury of Discoveries CMS Migration")
    canvas.drawRightString(width - 20 * mm, 11 * mm, f"Page {doc.page}")
    canvas.restoreState()


def styles():
    base = getSampleStyleSheet()
    return {
        "h1": ParagraphStyle("H1", parent=base["Heading1"], fontName="Helvetica-Bold", fontSize=20, leading=24, textColor=BLUE, spaceBefore=10, spaceAfter=9),
        "h2": ParagraphStyle("H2", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=14, leading=18, textColor=TEAL, spaceBefore=12, spaceAfter=6, keepWithNext=True),
        "h3": ParagraphStyle("H3", parent=base["Heading3"], fontName="Helvetica-Bold", fontSize=11, leading=14, textColor=BLUE, spaceBefore=9, spaceAfter=4, keepWithNext=True),
        "body": ParagraphStyle("Body", parent=base["BodyText"], fontName="Helvetica", fontSize=8.7, leading=12.2, textColor=INK, spaceAfter=5),
        "bullet": ParagraphStyle("Bullet", parent=base["BodyText"], fontName="Helvetica", fontSize=8.5, leading=11.5, leftIndent=12, firstLineIndent=-7, bulletIndent=3, spaceAfter=2.5),
        "quote": ParagraphStyle("Quote", parent=base["BodyText"], fontName="Helvetica-Oblique", fontSize=8.7, leading=12, textColor=BLUE, backColor=PALE, borderColor=TEAL, borderWidth=0.5, borderPadding=7, spaceBefore=4, spaceAfter=8),
        "code": ParagraphStyle("Code", fontName="Courier", fontSize=7, leading=9, textColor=INK, backColor=colors.HexColor("#F4F6F7"), borderPadding=6, spaceBefore=3, spaceAfter=7),
        "small": ParagraphStyle("Small", parent=base["BodyText"], fontSize=8, leading=10.5, textColor=colors.HexColor("#52606D")),
    }


def parse_table(lines: list[str], st: dict) -> Table:
    rows = []
    for line in lines:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-+:?", c) for c in cells):
            continue
        rows.append([Paragraph(inline(c), st["small"]) for c in cells])
    count = max((len(r) for r in rows), default=1)
    widths = [170 * mm / count] * count
    table = Table(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BLUE),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#B8C7CE")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F6F9FA")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def architecture_diagram(st: dict) -> Table:
    box = ParagraphStyle("DiagramBox", parent=st["body"], fontSize=9, leading=12, alignment=TA_CENTER, textColor=colors.white)
    arrow = ParagraphStyle("DiagramArrow", parent=st["body"], fontSize=12, alignment=TA_CENTER, textColor=TEAL)
    data = [[
        Paragraph("<b>Treasury of Discoveries</b><br/>Gallery / exhibit", box),
        Paragraph("→<br/><font size=6>Gallery Reference</font>", arrow),
        Paragraph("<b>TOD Photo Sets</b><br/>Artifact / subject", box),
        Paragraph("→<br/><font size=6>Photo Set Reference</font>", arrow),
        Paragraph("<b>TOD Gallery Photos</b><br/>Individual image", box),
    ]]
    table = Table(data, colWidths=[37 * mm, 27 * mm, 34 * mm, 29 * mm, 37 * mm], rowHeights=[21 * mm], hAlign="CENTER")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), BLUE),
        ("BACKGROUND", (2, 0), (2, 0), TEAL),
        ("BACKGROUND", (4, 0), (4, 0), BLUE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOX", (0, 0), (0, 0), 0.7, BLUE),
        ("BOX", (2, 0), (2, 0), 0.7, TEAL),
        ("BOX", (4, 0), (4, 0), 0.7, BLUE),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def story(markdown: str):
    st = styles()
    flow = []
    lines = markdown.splitlines()
    i = 0
    first_h1 = True
    while i < len(lines):
        raw = lines[i]
        if raw.startswith("```"):
            language = raw[3:].strip()
            i += 1
            block = []
            while i < len(lines) and not lines[i].startswith("```"):
                block.append(lines[i])
                i += 1
            if language == "mermaid":
                flow.extend([architecture_diagram(st), Spacer(1, 7)])
            else:
                label = f"{language.upper()}" if language else ""
                parts = ([Paragraph(f"<b>{label}</b>", st["small"])] if label else []) + [Preformatted("\n".join(block), st["code"])]
                flow.append(KeepTogether(parts))
        elif raw.startswith("# "):
            title = raw[2:]
            if first_h1:
                flow.extend([
                    Spacer(1, 36 * mm),
                    HRFlowable(width="36%", thickness=3, color=GOLD, hAlign="CENTER"),
                    Spacer(1, 10 * mm),
                    Paragraph(inline(title), ParagraphStyle("Title", parent=st["h1"], fontSize=28, leading=34, alignment=TA_CENTER, textColor=BLUE)),
                    Spacer(1, 8 * mm),
                    Paragraph("Project handoff and operational guide", ParagraphStyle("Sub", parent=st["body"], fontSize=13, leading=17, alignment=TA_CENTER, textColor=TEAL)),
                    Spacer(1, 18 * mm),
                    Paragraph("WordPress / extracted source -> Webflow CMS", ParagraphStyle("Tag", parent=st["body"], fontSize=10, alignment=TA_CENTER, textColor=INK)),
                    Spacer(1, 50 * mm),
                    Paragraph("Prepared 1 August 2026", ParagraphStyle("Date", parent=st["small"], alignment=TA_CENTER)),
                    PageBreak(),
                ])
                first_h1 = False
            else:
                flow.extend([PageBreak(), Paragraph(inline(title), st["h1"])])
        elif raw.startswith("## "):
            flow.append(Paragraph(inline(raw[3:]), st["h1"]))
        elif raw.startswith("### "):
            flow.append(Paragraph(inline(raw[4:]), st["h2"]))
        elif raw.startswith("#### "):
            flow.append(Paragraph(inline(raw[5:]), st["h3"]))
        elif raw.startswith("> "):
            flow.append(Paragraph(inline(raw[2:]), st["quote"]))
        elif raw.startswith("| "):
            tbl = []
            while i < len(lines) and lines[i].startswith("|"):
                tbl.append(lines[i])
                i += 1
            flow.append(parse_table(tbl, st))
            flow.append(Spacer(1, 5))
            continue
        elif re.match(r"^(?:- |\d+\. )", raw):
            marker, text = re.split(r"\s+", raw, maxsplit=1)
            bullet = "•" if marker == "-" else marker
            flow.append(Paragraph(inline(text), st["bullet"], bulletText=bullet))
        elif raw.strip() == "---":
            flow.append(HRFlowable(width="100%", thickness=0.7, color=GOLD, spaceBefore=5, spaceAfter=8))
        elif raw.strip():
            flow.append(Paragraph(inline(raw.replace("  ", " ")), st["body"]))
        i += 1
    return flow


def main() -> None:
    markdown = SOURCE.read_text(encoding="utf-8")
    doc = BaseDocTemplate(
        str(OUTPUT), pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=18 * mm, bottomMargin=22 * mm,
        title="Treasury of Discoveries CMS Migration - Project Handoff",
        author="The Kabilin Center migration handoff",
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="body")
    doc.addPageTemplates(PageTemplate(id="main", frames=[frame], onPage=page))
    doc.build(story(markdown))
    print(OUTPUT)


if __name__ == "__main__":
    main()
