"""Create operator-facing Word versions of analytical results."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

FONT = "Times New Roman"
INK = RGBColor(0, 0, 0)
MUTED = RGBColor(89, 89, 89)


def _font(run, size=14, bold=None, italic=None, color=INK):
    run.font.name = FONT
    run.font.size = Pt(size)
    run.font.color.rgb = color
    if bold is not None:
        run.bold = bold
    if italic is not None:
        run.italic = italic
    fonts = run._element.get_or_add_rPr().get_or_add_rFonts()
    for key in ("ascii", "hAnsi", "eastAsia", "cs"):
        fonts.set(qn(f"w:{key}"), FONT)
    return run


def _configure_style(style, size=14, bold=False, before=0, after=6, line=1.5):
    style.font.name = FONT
    style.font.size = Pt(size)
    style.font.bold = bold
    style.font.color.rgb = INK
    fonts = style._element.get_or_add_rPr().get_or_add_rFonts()
    for key in ("ascii", "hAnsi", "eastAsia", "cs"):
        fonts.set(qn(f"w:{key}"), FONT)
    fmt = style.paragraph_format
    fmt.space_before = Pt(before)
    fmt.space_after = Pt(after)
    fmt.line_spacing = line


def _page_number(paragraph):
    paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar"); begin.set(qn("w:fldCharType"), "begin")
    instruction = OxmlElement("w:instrText"); instruction.set(qn("xml:space"), "preserve"); instruction.text = " PAGE "
    separate = OxmlElement("w:fldChar"); separate.set(qn("w:fldCharType"), "separate")
    text = OxmlElement("w:t"); text.text = "1"
    end = OxmlElement("w:fldChar"); end.set(qn("w:fldCharType"), "end")
    for element in (begin, instruction, separate, text, end):
        run._r.append(element)
    _font(run, size=10, color=MUTED)


def _prepare(title, subtitle=""):
    document = Document()
    section = document.sections[0]
    section.page_width = Cm(21)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(2)
    section.bottom_margin = Cm(2)
    section.left_margin = Cm(3)
    section.right_margin = Cm(1.5)
    section.header_distance = Cm(1)
    section.footer_distance = Cm(1)

    styles = document.styles
    _configure_style(styles["Normal"], 14, False, 0, 6, 1.5)
    styles["Normal"].paragraph_format.first_line_indent = Cm(1.25)
    _configure_style(styles["Heading 1"], 16, True, 12, 6, 1.15)
    _configure_style(styles["Heading 2"], 14, True, 10, 5, 1.15)
    _configure_style(styles["Heading 3"], 14, True, 8, 4, 1.15)
    for name in ("List Bullet", "List Number"):
        _configure_style(styles[name], 14, False, 0, 4, 1.5)
        styles[name].paragraph_format.first_line_indent = None
        styles[name].paragraph_format.left_indent = Cm(1.25)

    header = section.header.paragraphs[0]
    header.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    _font(header.add_run("ЗОЛОТИЙ ПОНЧИК · АНАЛІТИЧНИЙ РЕЗУЛЬТАТ"), 9, bold=True, color=MUTED)
    _page_number(section.footer.paragraphs[0])

    paragraph = document.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.space_after = Pt(6)
    _font(paragraph.add_run(title.strip() or "Аналітичний результат"), 16, bold=True)
    if subtitle:
        paragraph = document.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.paragraph_format.first_line_indent = None
        paragraph.paragraph_format.space_after = Pt(12)
        _font(paragraph.add_run(subtitle), 12, color=MUTED)
    document.core_properties.title = title.strip() or "Аналітичний результат"
    document.core_properties.author = "Золотий пончик"
    return document


INLINE = re.compile(r"(\*\*.+?\*\*|(?<!\*)\*[^*]+?\*(?!\*)|\[[^]]+\]\(https?://[^)]+\))")


def _inline(paragraph, text):
    position = 0
    for match in INLINE.finditer(text):
        if match.start() > position:
            _font(paragraph.add_run(text[position:match.start()]))
        token = match.group(0)
        if token.startswith("**"):
            _font(paragraph.add_run(token[2:-2]), bold=True)
        elif token.startswith("*"):
            _font(paragraph.add_run(token[1:-1]), italic=True)
        else:
            label, url = re.match(r"\[([^]]+)\]\((https?://[^)]+)\)", token).groups()
            _font(paragraph.add_run(f"{label} ({url})"))
        position = match.end()
    if position < len(text):
        _font(paragraph.add_run(text[position:]))


def markdown_to_docx(markdown, output_path, title="Аналітичний результат", subtitle=""):
    document = _prepare(title, subtitle)
    lines = str(markdown or "").replace("\r\n", "\n").split("\n")
    paragraph_buffer = []

    def flush():
        if not paragraph_buffer:
            return
        paragraph = document.add_paragraph()
        _inline(paragraph, " ".join(part.strip() for part in paragraph_buffer).strip())
        paragraph_buffer.clear()

    in_code = False
    for raw in lines:
        line = raw.rstrip()
        if line.strip().startswith("```"):
            flush(); in_code = not in_code; continue
        if in_code:
            flush()
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.first_line_indent = None
            _font(paragraph.add_run(line), 11)
            continue
        if not line.strip():
            flush(); continue
        heading = re.match(r"^(#{1,3})\s+(.+)$", line.strip())
        bullet = re.match(r"^[-+*]\s+(.+)$", line.strip())
        numbered = re.match(r"^\d+[.)]\s+(.+)$", line.strip())
        if heading:
            flush()
            paragraph = document.add_paragraph(style=f"Heading {len(heading.group(1))}")
            paragraph.paragraph_format.first_line_indent = None
            _inline(paragraph, heading.group(2))
        elif bullet or numbered:
            flush()
            paragraph = document.add_paragraph(style="List Bullet" if bullet else "List Number")
            _inline(paragraph, (bullet or numbered).group(1))
        elif line.lstrip().startswith(">"):
            flush()
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.left_indent = Cm(1.25)
            paragraph.paragraph_format.first_line_indent = None
            _inline(paragraph, line.lstrip()[1:].strip())
            for run in paragraph.runs:
                run.italic = True
        else:
            paragraph_buffer.append(line)
    flush()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(output_path)
    return output_path


def calendar_to_docx(rows, output_path, title, subtitle=""):
    document = _prepare(title, subtitle)
    if not rows:
        paragraph = document.add_paragraph("Подій за обраний період не знайдено.")
        paragraph.paragraph_format.first_line_indent = None
    for row in rows:
        heading = document.add_paragraph(style="Heading 2")
        heading.paragraph_format.first_line_indent = None
        _inline(heading, str(row.get("title") or "Подія"))
        starts = row.get("starts_at") or "Дата уточнюється"
        meta = document.add_paragraph()
        meta.paragraph_format.first_line_indent = None
        _font(meta.add_run(f"Дата: {starts} · Статус: {row.get('status','—')}"), 12, bold=True)
        if row.get("location"):
            _font(meta.add_run(f" · Місце: {row['location']}"), 12)
        if row.get("summary"):
            paragraph = document.add_paragraph()
            _inline(paragraph, str(row["summary"]))
    document.save(output_path)
    return Path(output_path)
