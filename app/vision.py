"""Vision fallback for scanned statements: Claude reads the table.

The geometry engine reads a scan through OCR word boxes, and on some scans that
is not enough - a header Tesseract cannot read, rows that fuse, a table that runs
across pages. When the failsafe does not reconcile a scan, the PDF is sent to
Claude, which reads the table the way a person would.

Claude only *reads*. It returns what is printed: the header labels verbatim, each
row as cells aligned to those labels, and whether a row is data, a subtotal or a
total. Everything that decides whether the figures can leave the app stays in
code and stays independent of the model:

- **Which column is the commission** comes from infer_canonical() on the printed
  header labels - the same rule every auto-detected layout uses. Claude is never
  asked which column pays, so it cannot pick whichever column happens to match.
- **The failsafe references** are the filename amount and the totals found in
  the *OCR* text of the page. Claude never sees the filename, and its own reading
  of the totals row is only used for the in-table Checks, never as a reference.
  A misread that comes out self-consistent therefore still fails against the
  filename.
- **No figure is adjusted.** The prompt forbids changing a figure to make totals
  agree, for the same reason as the deposit slips: a model allowed to "fix" a
  digit would turn every misread into a false pass.

The PDF goes to the Anthropic API, so this needs ANTHROPIC_API_KEY and means the
statement leaves this machine. When Claude is not available the geometry result
stands unchanged.
"""

from __future__ import annotations

import base64
import json

from . import image_ocr

# The API reads a base64 PDF up to 32MB per request. Statement scans here are
# 60-800KB, so this only guards against an unexpected upload.
MAX_PDF_BYTES = 30 * 1024 * 1024

SYSTEM_PROMPT = """You read insurance commission statements and report the table on them exactly as printed.

What to report:
- `columns`: the table's column headings, left to right, exactly as printed. One entry per printed column. If a heading wraps over two lines, join the lines with a space ("POLICY" over "NUMBER" becomes "POLICY NUMBER").
- `rows`: every line of the table body, top to bottom, on every page. When the table continues on another page with the same columns, carry on in the same list. Each row's `cells` has exactly one entry per column, in the same order; use "" for a blank cell.
- `kind` for each row: "total" for the statement's grand total line, "subtotal" for a total that closes a group or a policy, "data" for everything else.
- `statements`: how many separate statements the document holds (for example, two agents' statements in one file). Normally 1.
- `uncertain`: anything you could not read with confidence, one short note each.

Rules:
- Copy every figure exactly as printed, including commas, a trailing or leading minus, and parentheses. Do not compute, round or reformat anything.
- Never change a figure so that a total agrees. The arithmetic is checked separately, and an adjusted figure would hide a misreading.
- If a digit is ambiguous, give the most likely reading and describe the doubt in `uncertain`.
- Put each value under the column it is printed in, even when neighbouring columns are close together or the grid lines are faint.
- Do not include the page heading, the address block, or lines outside the table."""

INSTRUCTION = "Read the commission table in this statement."

_STR = {"type": "string"}
SCHEMA = {
    "type": "object",
    "properties": {
        "columns": {"type": "array", "items": _STR},
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["data", "subtotal", "total"]},
                    "cells": {"type": "array", "items": _STR},
                },
                "required": ["kind", "cells"],
                "additionalProperties": False,
            },
        },
        "statements": {"type": "integer"},
        "uncertain": {"type": "array", "items": _STR},
    },
    "required": ["columns", "rows", "statements", "uncertain"],
    "additionalProperties": False,
}


class VisionError(Exception):
    """The vision read could not produce a table. Shown to the user as a warning."""


def available() -> tuple[bool, str]:
    """(usable, reason). Mirrors the Simple OCR check, so the UI says the same."""
    return image_ocr._claude_available()


def read_table(path: str) -> dict:
    """Send the PDF to Claude and return its reading.

    {"columns": [...], "rows": [{"kind", "cells"}], "statements": int,
     "uncertain": [...], "model": str}
    """
    with open(path, "rb") as f:
        data = f.read()
    if len(data) > MAX_PDF_BYTES:
        raise VisionError("The PDF is too large to send for a vision read.")

    document = {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.standard_b64encode(data).decode("ascii"),
        },
    }
    try:
        response = image_ocr.call_claude(
            [document], SYSTEM_PROMPT, INSTRUCTION,
            # Printed digits on a poor scan are the whole job; spend the thinking.
            output_config={
                "effort": "high",
                "format": {"type": "json_schema", "schema": SCHEMA},
            },
        )
    except image_ocr.OCRError as exc:
        raise VisionError(str(exc)) from exc

    if response.stop_reason == "max_tokens":
        raise VisionError("The vision read was cut off before it finished.")
    try:
        table = json.loads(image_ocr.text_of(response))
    except ValueError as exc:
        raise VisionError("The model returned an unreadable answer.") from exc
    table["model"] = response.model
    return table


def to_rows(table: dict) -> tuple[list[str], list[dict], list[dict], list[str]]:
    """(columns, data rows, total rows, problems) as the Statement wants them.

    A row whose cell count differs from the header count is not padded or
    trimmed into place - shifting its cells would put figures under the wrong
    column, which is exactly the fault this fallback exists to fix. It is
    reported instead and left out.
    """
    columns = _unique([c.strip() for c in table.get("columns", [])])
    data: list[dict] = []
    totals: list[dict] = []
    problems: list[str] = []
    for n, row in enumerate(table.get("rows", []), start=1):
        cells = row.get("cells", [])
        if len(cells) != len(columns):
            problems.append(
                f"row {n} has {len(cells)} cells for {len(columns)} columns"
            )
            continue
        record = {c: (v or "").strip() for c, v in zip(columns, cells)}
        if row.get("kind") == "data":
            data.append(record)
        else:
            totals.append({**record, "_kind": row.get("kind", "")})
    return columns, data, totals, problems


def _unique(names: list[str]) -> list[str]:
    """Rows are dicts keyed by column name, so two identical headings would
    silently overwrite each other's cells. Suffix repeats instead."""
    seen: dict[str, int] = {}
    out = []
    for name in names:
        name = name or "Column"
        seen[name] = seen.get(name, 0) + 1
        out.append(name if seen[name] == 1 else f"{name} ({seen[name]})")
    return out
