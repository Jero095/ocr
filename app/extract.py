"""Extract commission-statement tables from PDFs.

The sample statements are digital PDFs stored with a 180-degree page rotation:
every string is stored in reversed character order and the visual reading order
runs down the page rather than across. We un-rotate by reversing each word and
grouping on x0 (which becomes the row axis), then infer column boundaries from
the header row so the parser tolerates layout shifts within a template family.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field, asdict

import pdfplumber

# Words in the header row whose presence identifies it.
HEADER_ANCHOR = re.compile(r"^Policy\b", re.I)
# Rows whose first cell looks like a policy number.
POLICY_RE = re.compile(r"^[A-Z]{2,4}\d{4,}", re.I)
# Gap (in points) between header column groups. Intra-column word gaps in the
# sample are <=24pt and inter-column gaps are >=35pt, so 30 separates cleanly.
COLUMN_GAP = 30

NUMERIC_COLUMNS = {"Gross", "Comm $", "Paid", "Adj", "Net"}


@dataclass
class Check:
    label: str
    rows_total: float
    stated_total: float

    @property
    def ok(self) -> bool:
        return abs(self.rows_total - self.stated_total) < 0.01


@dataclass
class Statement:
    filename: str
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, str]] = field(default_factory=list)
    totals: dict[str, str] = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "columns": self.columns,
            "rows": self.rows,
            "totals": self.totals,
            "warnings": self.warnings,
            "checks": [
                {**asdict(c), "ok": c.ok} for c in self.checks
            ],
        }

    def to_tsv(self) -> str:
        lines = ["\t".join(self.columns)]
        lines += ["\t".join(r.get(c, "") for c in self.columns) for r in self.rows]
        if self.totals:
            lines.append("\t".join(self.totals.get(c, "") for c in self.columns))
        return "\n".join(lines) + "\n"


def _clean(text: str) -> str:
    """Repair artifacts the rotated text layer introduces."""
    text = re.sub(r"\(\s*(.*?)\s*\)", r"(\1)", text)        # ( 75.00 ) -> (75.00)
    text = re.sub(r"(?<=[\d.,])\s+(?=[\d.,])", "", text)     # 150.0 0  -> 150.00
    return text.strip()


def _to_float(text: str) -> float | None:
    """Parse a statement number. Parentheses denote a negative."""
    s = text.strip()
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "").replace("%", "").replace("$", "")
    try:
        value = float(s)
    except ValueError:
        return None
    return -value if negative else value


def _lines_from_page(page) -> list[list[tuple[str, float]]]:
    """Return the page as lines of (text, position) pairs, in reading order.

    For a 180-rotated page, x0 indexes the visual row and `top` indexes the
    visual column, so we group on x0 and sort within a group by descending top.
    """
    words = page.extract_words()
    rotated = (page.rotation or 0) % 360 == 180

    buckets: dict[int, list] = defaultdict(list)
    for w in words:
        key = round(w["x0"] / 3) if rotated else round(w["top"] / 3)
        buckets[key].append(w)

    lines = []
    for key in sorted(buckets):
        group = buckets[key]
        if rotated:
            group.sort(key=lambda w: -w["top"])
            lines.append([(w["text"][::-1], w["top"]) for w in group])
        else:
            group.sort(key=lambda w: w["x0"])
            lines.append([(w["text"], w["x0"]) for w in group])
    return lines


def _infer_columns(header: list[tuple[str, float]], rotated: bool):
    """Group header words into columns and derive boundaries between them.

    Returns (names, bounds) where bounds[i] is the (lo, hi) position range that
    selects words belonging to column i.
    """
    groups: list[list[tuple[str, float]]] = [[header[0]]]
    for text, pos in header[1:]:
        if abs(pos - groups[-1][-1][1]) > COLUMN_GAP:
            groups.append([(text, pos)])
        else:
            groups[-1].append((text, pos))

    names = [
        " ".join(t for t, _ in g).replace("*", "").replace(" #", " #").strip()
        for g in groups
    ]

    positions = [[p for _, p in g] for g in groups]
    bounds = []
    for i, pos in enumerate(positions):
        if rotated:
            # positions descend across columns
            hi = float("inf") if i == 0 else (min(positions[i - 1]) + max(pos)) / 2
            lo = 0.0 if i == len(positions) - 1 else (min(pos) + max(positions[i + 1])) / 2
        else:
            lo = float("-inf") if i == 0 else (max(positions[i - 1]) + min(pos)) / 2
            hi = float("inf") if i == len(positions) - 1 else (max(pos) + min(positions[i + 1])) / 2
        bounds.append((lo, hi))
    return names, bounds


def parse_pdf(path: str, filename: str) -> Statement:
    stmt = Statement(filename=filename)

    with pdfplumber.open(path) as pdf:
        # Rotation is per-page, so carry it alongside each line: a statement can
        # mix a rotated table page with an unrotated cover or address page.
        all_lines: list[tuple[list[tuple[str, float]], bool]] = []
        for page in pdf.pages:
            page_rotated = (page.rotation or 0) % 360 == 180
            for line in _lines_from_page(page):
                all_lines.append((line, page_rotated))

    header_line = None
    rotated = False
    for line, page_rotated in all_lines:
        if line and HEADER_ANCHOR.match(line[0][0]):
            header_line, rotated = line, page_rotated
            break

    if header_line is None:
        stmt.warnings.append(
            "No 'Policy' header row found - this layout is not recognised yet."
        )
        return stmt

    names, bounds = _infer_columns(header_line, rotated)
    stmt.columns = names

    def cells(line):
        out = {}
        for name, (lo, hi) in zip(names, bounds):
            picked = [t for t, p in line if lo <= p < hi]
            out[name] = _clean(" ".join(picked))
        return out

    for line, page_rotated in all_lines:
        if not line or page_rotated != rotated or line is header_line:
            continue
        joined = " ".join(t for t, _ in line)
        if POLICY_RE.match(joined):
            stmt.rows.append(cells(line))
        elif joined.startswith("Totals"):
            row = cells(line)
            # "Totals" sits under a data column; move it to the first column.
            for k, v in row.items():
                if v.startswith("Totals"):
                    row[k] = ""
            row[names[0]] = "Totals"
            stmt.totals = row

    if not stmt.rows:
        stmt.warnings.append("Header found but no policy rows matched.")

    # Cross-check row sums against the statement's own stated totals.
    for col in names:
        if col not in NUMERIC_COLUMNS or col not in stmt.totals:
            continue
        stated = _to_float(stmt.totals.get(col, ""))
        if stated is None:
            continue
        values = [_to_float(r.get(col, "")) for r in stmt.rows]
        values = [v for v in values if v is not None]
        if values:
            stmt.checks.append(Check(col, round(sum(values), 2), stated))

    if not stmt.totals:
        stmt.warnings.append("No totals row found - could not cross-check arithmetic.")

    for row in stmt.rows:
        for col in ("Gross", "Comm%", "Comm $"):
            if col not in row:
                break
        else:
            gross, pct, comm = (_to_float(row[c]) for c in ("Gross", "Comm%", "Comm $"))
            if None not in (gross, pct, comm) and abs(gross * pct / 100 - comm) > 0.01:
                stmt.warnings.append(
                    f"Row {row.get(names[0], '?')} {row.get('Invoice #', '')}: "
                    f"{gross} x {pct}% != {comm}"
                )

    return stmt
