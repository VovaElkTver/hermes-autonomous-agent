"""Minimal, dependency-free xlsx read/write using only the standard library.

``.xlsx`` is an OPC (ZIP) package of XML parts. For this harness we only need a
single worksheet whose cells we can read and write, so we generate/parse just the
required parts. Inline strings are used (no sharedStrings part).

Enough for the claims-harness demos; not a general-purpose xlsx library.
"""

from __future__ import annotations

import re
import zipfile
from typing import Dict, Optional, Tuple
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CELL_RE = re.compile(r"([A-Z]+)(\d+)")


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #
def read_xlsx(path: str) -> Dict[Tuple[str, int], object]:
    """Read the first worksheet into ``{(col, row): value}``.

    Numeric cells become ``float``; inline strings become ``str``.
    """
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        sheet = _find_sheet_part(names)
        data = z.read(sheet)
    root = ET.fromstring(data)
    cells: Dict[Tuple[str, int], object] = {}
    for c in root.iter(f"{{{_NS}}}c"):
        ref = c.get("r")
        if not ref:
            continue
        m = _CELL_RE.match(ref)
        if not m:
            continue
        col, row = m.group(1), int(m.group(2))
        t = c.get("t")
        if t == "inlineStr":
            is_el = c.find(f"{{{_NS}}}is")
            if is_el is not None:
                t_el = is_el.find(f"{{{_NS}}}t")
                cells[(col, row)] = t_el.text if t_el is not None else ""
            continue
        v_el = c.find(f"{{{_NS}}}v")
        if v_el is None:
            continue
        text = v_el.text
        try:
            cells[(col, row)] = float(text)
        except (TypeError, ValueError):
            cells[(col, row)] = text
    return cells


def _find_sheet_part(names):
    for n in names:
        if re.match(r"xl/worksheets/sheet\d+\.xml$", n):
            return n
    raise ValueError("no worksheet part found in xlsx")


def col_index(col: str) -> int:
    """``'A' -> 0``, ``'Z' -> 25``, ``'AA' -> 26``."""
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


# --------------------------------------------------------------------------- #
# Write (minimal single-sheet writer, inline strings)
# --------------------------------------------------------------------------- #
_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
    "</Types>"
)

_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    "</Relationships>"
)

_WORKBOOK = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<workbook xmlns="{ns}" xmlns:r="{rns}">'
    '<sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets>'
    "</workbook>"
).format(ns=_NS, rns=_REL_NS)

_WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
    "</Relationships>"
)

_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<styleSheet xmlns="{_NS}">'
    "<fonts count=\"1\"><font/></fonts>"
    "<fills count=\"1\"><fill/></fills>"
    "<borders count=\"1\"><border/></borders>"
    "<cellStyleXfs count=\"1\"><xf/></cellStyleXfs>"
    "<cellXfs count=\"1\"><xf/></cellXfs>"
    "</styleSheet>"
)


def _cell_xml(ref: str, value: object) -> str:
    if isinstance(value, str):
        return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(value)}</t></is></c>'
    return f'<c r="{ref}"><v>{value}</v></c>'


def write_xlsx(path: str, cells: Dict[Tuple[str, int], object]) -> None:
    """Write a single-sheet workbook from ``{(col, row): value}``."""
    rows: Dict[int, Dict[str, str]] = {}
    for (col, row), value in cells.items():
        rows.setdefault(row, {})[col] = _cell_xml(f"{col}{row}", value)

    rows_xml = "".join(
        f'<row r="{r}">{"".join(rows[r][c] for c in sorted(rows[r], key=col_index))}</row>'
        for r in sorted(rows)
    )
    sheet_xml = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="{_NS}"><sheetData>{rows_xml}</sheetData></worksheet>'

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("xl/workbook.xml", _WORKBOOK)
        z.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        z.writestr("xl/styles.xml", _STYLES)
        z.writestr("xl/worksheets/sheet1.xml", sheet_xml)
