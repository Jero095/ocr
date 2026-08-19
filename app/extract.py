"""Extract commission-statement tables from PDFs.

Geometry is shared across carriers; per-carrier specifics live in TEMPLATES.

Two layout families are supported so far and they differ in almost every way,
which drove the design:

  Chris Leef  - 180-degree page rotation (every string stored in reversed
                character order, reading order runs down the page), single-line
                header, rows keyed by policy number.
  Vertigo     - unrotated, table on page 2, header wrapped across five lines,
                amounts prefixed with '$', rows keyed by payment date, with a
                per-date subtotal above a grand total.

Neither is OCR: both have intact text layers.

The column engine works in three steps:

  1. Normalise the axes. A rotated page's *visual* left-to-right runs along
     descending `top`, so spans are negated (-bottom, -top) and every
     downstream comparison is plain ascending interval maths.
  2. Find the header band (consecutive lines within BAND_GAP) and merge the
     word spans in it into columns. This handles wrapped headers for free.
  3. For each body line, merge words into contiguous runs, then assign each run
     to the column it overlaps most.

Step 3 is what makes one engine serve both layouts. Assigning individual words
by midpoint boundaries fails in opposite directions on the two statements:
Vertigo right-aligns data under left-aligned headers, and Chris Leef's insured
name is wider than its own header text. Merging a row's words into runs first
resolves both, because a run overlaps its true column even when single words
do not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

import pdfplumber

# --- geometry tunables -------------------------------------------------------
# Words within this distance on the row axis belong to the same line.
LINE_TOL = 3.0
# Consecutive lines within this distance form one header band. Measured: the
# widest intra-header line gap is 5.0 (Vertigo's wrapped header) and the
# narrowest header-to-data gap is 11.1, so 8 sits between them.
BAND_GAP = 8.0
# Header word spans closer than this merge into one column. Measured across the
# two known layouts: the widest gap inside a header label is 2.2 and the
# narrowest gap between two header labels is 5.3, so 4 sits between them. This
# is the tightest margin in the file - revisit it when adding a carrier.
# Data cells are never merged by distance; see _cells for why.
MERGE_GAP = 4.0


@dataclass(frozen=True)
class Template:
    """Per-carrier configuration. Geometry is shared; only these differ."""

    name: str
    # Lowercased words that must all appear in the header band.
    header_tokens: frozenset[str]
    # The first cell of a data row must match this.
    row_first_cell: re.Pattern
    # canonical field -> this template's column name, for cross-carrier export.
    canonical: dict[str, str] = field(default_factory=dict)
    # (base column, rate column, result column) for a base * rate == result check.
    rate_check: tuple[str, str, str] | None = None


TEMPLATES: list[Template] = [
    Template(
        name="Chris Leef General Agency",
        header_tokens=frozenset({"policy", "insured", "invoice", "gross"}),
        row_first_cell=re.compile(r"^[A-Z]{2,4}\d{4,}", re.I),
        canonical={
            "Policy Number": "Policy",
            "Insured Name": "Insured Name",
            "Effective Date": "Eff. Date",
            "Base Amount": "Gross",
            "Commission Rate": "Comm%",
            "Commission Amount": "Comm $",
        },
        rate_check=("Gross", "Comm%", "Comm $"),
    ),
    Template(
        name="Vertigo Insurance",
        header_tokens=frozenset({"payment", "named", "insured", "number", "accounting"}),
        row_first_cell=re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}\s*$"),
        canonical={
            "Policy Number": "Policy Number",
            "Insured Name": "Named Insured",
            "Effective Date": "Policy Effective Date",
            "Base Amount": "Total Commissionable",
            "Commission Rate": "Agency Commission Rate",
            "Commission Amount": "Total Commission Amount",
        },
        rate_check=(
            "Total Commissionable",
            "Agency Commission Rate",
            "Total Commission Amount",
        ),
    ),
]


CANONICAL_FIELDS = [
    "Policy Number",
    "Insured Name",
    "Effective Date",
    "Base Amount",
    "Commission Rate",
    "Commission Amount",
]

# Keyword patterns used to map an auto-detected carrier's columns onto the
# shared schema. First match wins, so order matters: "commission amount" must be
# tried before the looser "commission" that would also catch a rate column.
_CANONICAL_PATTERNS: list[tuple[str, list[str]]] = [
    ("Policy Number", [r"policy\s*(no|number|#)", r"^policy$", r"cert(ificate)?\s*(no|#)"]),
    ("Insured Name", [r"insured", r"named\s+insured", r"^name$", r"client", r"customer"]),
    ("Effective Date", [r"eff(ective)?\.?\s*date", r"^eff\.?$", r"policy\s+eff"]),
    ("Commission Amount", [
        # Payment columns first. Several carriers print commission *earned*
        # alongside what is actually paid this statement, and the declared total
        # is the payment: US Risk earns 1,696.15 but pays 961.02.
        r"current\s*pmt", r"pay\s*amt", r"net\s*pay", r"check\s*amount",
        r"comm(ission)?\s*(amount|amt|\$|paid|due)", r"total\s+commission",
        r"^comm\s*\$$", r"^commission$",
        # Observed variants across the carriers in statements/. Kept specific:
        # a pattern that grabs the wrong column produces a false PASS, which is
        # worse than reporting that the failsafe could not run.
        r"^comm(ission)?s?$", r"^paid$",
        r"comm\.?\s*amt", r"net\s*comm", r"paid\s*amount", r"amt\.?\s*paid",
        r"line\s*total", r"to\s*agent", r"^amount$",
    ]),
    ("Commission Rate", [r"comm(ission)?\s*(rate|%)", r"rate", r"comm%", r"%"]),
    ("Base Amount", [
        r"commissionable", r"premium", r"^gross$", r"gross\s+premium", r"^base$",
    ]),
]


def infer_canonical(columns: list[str]) -> dict[str, str]:
    """Guess a canonical mapping from column names, for auto-detected layouts.

    Without this the cross-carrier sheet would silently contain only the
    hand-written templates. A field is left unmapped rather than guessed loosely.
    """
    mapping: dict[str, str] = {}
    taken: set[str] = set()
    for field_name, patterns in _CANONICAL_PATTERNS:
        for pattern in patterns:
            hit = next(
                (
                    c for c in columns
                    if c not in taken and re.search(pattern, c.strip(), re.I)
                ),
                None,
            )
            if hit:
                mapping[field_name] = hit
                taken.add(hit)
                break
    return mapping


def canonical_map(template_name: str, columns: list[str] | None = None) -> dict[str, str]:
    """canonical field -> that carrier's column name.

    Hand-written templates declare this explicitly; auto-detected layouts fall
    back to inferring it from their column names.
    """
    for t in TEMPLATES:
        if t.name == template_name:
            return t.canonical
    return infer_canonical(columns or [])


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
    template: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, str]] = field(default_factory=list)
    totals: dict[str, str] = field(default_factory=dict)
    subtotals: list[dict[str, str]] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    payout: PayoutCheck | None = None
    warnings: list[str] = field(default_factory=list)
    # Delimited (CSV/TSV) sources only: what the cleaner changed, and how the
    # file was split. Empty for PDFs.
    cleanup: list[dict] = field(default_factory=list)
    delimiter: str = ""

    @property
    def canonical(self) -> dict[str, str]:
        for t in TEMPLATES:
            if t.name == self.template:
                return t.canonical
        return {}

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "template": self.template,
            "columns": self.columns,
            "rows": self.rows,
            "totals": self.totals,
            "subtotals": self.subtotals,
            "warnings": self.warnings,
            "checks": [{**asdict(c), "ok": c.ok} for c in self.checks],
            "payout": self.payout.to_dict() if self.payout else None,
            "cleanup": self.cleanup,
            "delimiter": self.delimiter,
        }

    def to_tsv(self) -> str:
        lines = ["\t".join(self.columns)]
        lines += ["\t".join(r.get(c, "") for c in self.columns) for r in self.rows]
        if self.totals:
            lines.append("\t".join(self.totals.get(c, "") for c in self.columns))
        return "\n".join(lines) + "\n"


# --- text helpers ------------------------------------------------------------

def _clean(text: str) -> str:
    """Repair artifacts the rotated text layer introduces."""
    text = re.sub(r"\(\s*(.*?)\s*\)", r"(\1)", text)      # ( 75.00 ) -> (75.00)
    text = re.sub(r"(?<=[\d.,])\s+(?=[\d.,])", "", text)   # 150.0 0  -> 150.00
    return text.strip()


def to_float(text: str) -> float | None:
    """Parse a statement amount. Parentheses denote a negative; $ and % strip.

    Returns None for anything that is not a plain number, which is how dates
    like '6/1/26' and '26-Jun' are kept out of the numeric checks.
    """
    s = (text or "").strip()
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "").replace("$", "").replace("%", "").strip()
    try:
        value = float(s)
    except ValueError:
        return None
    return -value if negative else value


def is_percent(text: str) -> bool:
    return "%" in (text or "")


# --- geometry ----------------------------------------------------------------

def _row_axis(word, rotated: bool) -> float:
    """Position along the axis that separates one line from the next."""
    return word["x0"] if rotated else word["top"]


def _span(word, rotated: bool) -> tuple[float, float]:
    """Extent along the axis that separates one column from the next.

    Negated for rotated pages so that ascending order always means visual
    left-to-right and interval maths is uniform across both orientations.
    """
    if rotated:
        return (-word["bottom"], -word["top"])
    return (word["x0"], word["x1"])


def _text(word, rotated: bool) -> str:
    return word["text"][::-1] if rotated else word["text"]


def _merge_spans(spans: list[tuple[float, float]], gap: float):
    """Merge overlapping or near-touching intervals. Input must be sorted."""
    merged: list[list[float]] = []
    for lo, hi in spans:
        if merged and lo - merged[-1][1] <= gap:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [(lo, hi) for lo, hi in merged]


def _lines(words, rotated: bool) -> list[list[dict]]:
    """Group words into lines, ordered visually top-to-bottom."""
    if not words:
        return []
    ordered = sorted(words, key=lambda w: _row_axis(w, rotated))
    out: list[list[dict]] = [[ordered[0]]]
    for w in ordered[1:]:
        if _row_axis(w, rotated) - _row_axis(out[-1][0], rotated) <= LINE_TOL:
            out[-1].append(w)
        else:
            out.append([w])
    for line in out:
        line.sort(key=lambda w: _span(w, rotated)[0])
    return out


def _bands(lines: list[list[dict]], rotated: bool) -> list[list[dict]]:
    """Merge consecutive lines that sit within BAND_GAP into one band."""
    bands: list[list[dict]] = []
    prev: float | None = None
    for line in lines:
        pos = min(_row_axis(w, rotated) for w in line)
        if prev is not None and pos - prev <= BAND_GAP:
            bands[-1].extend(line)
        else:
            bands.append(list(line))
        prev = pos
    return bands


def _columns(band: list[dict], rotated: bool):
    """Derive column names and extents from the words of a header band."""
    spans = sorted(_span(w, rotated) for w in band)
    extents = _merge_spans(spans, MERGE_GAP)

    names = []
    for lo, hi in extents:
        members = [w for w in band if lo <= _span(w, rotated)[0] <= hi]
        # Wrapped headers read down first, then across.
        members.sort(key=lambda w: (_row_axis(w, rotated), _span(w, rotated)[0]))
        name = " ".join(_text(w, rotated) for w in members)
        names.append(name.replace("*", "").strip())
    return names, extents


def _cells(line: list[dict], names, extents, rotated: bool) -> dict[str, str]:
    """Assign a line's words to columns by maximum interval overlap.

    A word that overlaps no column at all is treated as a continuation of the
    previous word's cell rather than snapped to the nearest column. That is the
    wrapped/overflowing-text case (Chris Leef's insured name runs wider than its
    own header, so its last word lands in the gutter). Snapping to the nearest
    column instead would put it in the following column.

    Words are assigned individually, never pre-merged into runs: on the Chris
    Leef totals line adjacent figures in different columns sit 0.5pt apart, so
    any distance-based merge would fuse three columns into one.
    """
    out: dict[str, str] = {n: "" for n in names}
    previous: str | None = None

    for w in line:  # already in visual reading order
        lo, hi = _span(w, rotated)

        best, best_overlap = None, 0.0
        for idx, (clo, chi) in enumerate(extents):
            overlap = min(hi, chi) - max(lo, clo)
            if overlap > best_overlap:
                best, best_overlap = idx, overlap

        name = names[best] if best is not None else previous
        if name is None:
            continue  # leading word in a gutter with no cell to attach to

        text = _text(w, rotated)
        out[name] = f"{out[name]} {text}".strip() if out[name] else text
        previous = name

    return {k: _clean(v) for k, v in out.items()}


# --- parsing -----------------------------------------------------------------

def parse_pdf(path: str, filename: str) -> Statement:
    stmt = Statement(filename=filename)

    # Rotation is per page: a statement can mix a rotated table page with an
    # unrotated cover or address page, so each page is grouped independently.
    pages: list[tuple[list[list[dict]], bool]] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            rotated = (page.rotation or 0) % 360 == 180
            pages.append((_lines(page.extract_words(), rotated), rotated))

    found = None
    for lines, rotated in pages:
        for band in _bands(lines, rotated):
            tokens = {_text(w, rotated).lower().strip(":*") for w in band}
            for template in TEMPLATES:
                if template.header_tokens <= tokens:
                    found = (template, band, lines, rotated)
                    break
            if found:
                break
        if found:
            break

    if not found:
        if not any(line for lines, _ in pages for line in lines):
            stmt.warnings.append(
                "No text layer - this PDF is an image-only scan. It needs OCR, "
                "which this parser does not do."
            )
            _finalise(stmt, pages)
            return stmt

        auto = _auto_detect(pages)
        if auto is None:
            stmt.warnings.append(
                "Has a text layer but no table could be located. Needs a "
                "template in extract.py, or the LLM fallback."
            )
            _finalise(stmt, pages)
            return stmt

        names, rows, totals, subs, checks = auto
        stmt.template = f"{carrier_from_filename(filename)} (auto-detected)"
        stmt.columns, stmt.rows = names, rows
        stmt.totals, stmt.subtotals, stmt.checks = totals, subs, checks
        if not checks:
            stmt.warnings.append(
                "Auto-detected layout with no totals row to cross-check - "
                "figures are unverified. Review before use."
            )
        elif not all(c.ok for c in checks):
            stmt.warnings.append(
                "Auto-detected layout does not reconcile against its own "
                "totals. Review before use."
            )
        _finalise(stmt, pages)
        return stmt

    template, band, lines, rotated = found
    stmt.template = template.name
    names, extents = _columns(band, rotated)
    stmt.columns = names

    band_end = max(_row_axis(w, rotated) for w in band)
    totals_rows: list[dict[str, str]] = []

    for line in lines:
        if min(_row_axis(w, rotated) for w in line) <= band_end:
            continue  # header band or anything above it
        row = _cells(line, names, extents, rotated)
        joined = " ".join(row.values())

        if re.search(r"\btotals?\b", joined, re.I):
            # The label ("Totals" / "Grand Total") lands in whichever column it
            # sits under; move it to the first column so the row reads as a
            # total regardless of layout.
            label = ""
            for key, value in row.items():
                if re.search(r"\btotals?\b", value, re.I):
                    label = label or value
                    row[key] = ""
            row[names[0]] = label
            totals_rows.append(row)
        elif template.row_first_cell.match(row.get(names[0], "")):
            stmt.rows.append(row)

    if totals_rows:
        # The last totals-like line is the statement total; earlier ones are
        # per-group subtotals (Vertigo prints one per payment date).
        stmt.totals = totals_rows[-1]
        stmt.subtotals = totals_rows[:-1]
    else:
        stmt.warnings.append("No totals row found - could not cross-check arithmetic.")

    if not stmt.rows:
        # A template's header tokens can appear in an unrelated carrier's header
        # (Amwins' header contains all of Chris Leef's tokens). A match that
        # yields no rows is a false positive, so fall through to auto-detection.
        auto = _auto_detect(pages)
        if auto is not None:
            names, rows, totals, subs, checks = auto
            stmt.template = f"{carrier_from_filename(filename)} (auto-detected)"
            stmt.columns, stmt.rows = names, rows
            stmt.totals, stmt.subtotals, stmt.checks = totals, subs, checks
            if not checks:
                stmt.warnings.append(
                    "Auto-detected layout with no totals row to cross-check - "
                    "figures are unverified. Review before use."
                )
            elif not all(c.ok for c in checks):
                stmt.warnings.append(
                    "Auto-detected layout does not reconcile against its own "
                    "totals. Review before use."
                )
            _finalise(stmt, pages)
            return stmt
        stmt.warnings.append(
            f"Recognised the {template.name} header but matched no data rows."
        )

    _validate(stmt, template)
    _finalise(stmt, pages)
    return stmt


def _validate(stmt: Statement, template: Template) -> None:
    """Cross-check row sums and per-row rate arithmetic against the statement."""
    # A column is checkable when the totals row holds a number for it. That
    # detects numeric columns per layout instead of hardcoding a column list.
    for col in stmt.columns:
        stated = to_float(stmt.totals.get(col, ""))
        if stated is None:
            continue
        values = [to_float(r.get(col, "")) for r in stmt.rows]
        values = [v for v in values if v is not None]
        if values:
            stmt.checks.append(Check(col, round(sum(values), 2), stated))

    if not template.rate_check:
        return
    base_col, rate_col, result_col = template.rate_check
    if not {base_col, rate_col, result_col} <= set(stmt.columns):
        return
    for row in stmt.rows:
        base, rate, result = (to_float(row.get(c, "")) for c in (base_col, rate_col, result_col))
        if None in (base, rate, result):
            continue
        if abs(base * rate / 100 - result) > 0.01:
            label = row.get(stmt.columns[0], "?")
            stmt.warnings.append(
                f"Row {label}: {base} x {rate}% = {round(base * rate / 100, 2)}, "
                f"but {result_col} says {result}"
            )


# --- filename-based carrier naming ---------------------------------------------

def carrier_from_filename(filename: str) -> str:
    """'Big Sky 362.10.PDF' -> 'Big Sky'. Amount and duplicate suffix stripped."""
    stem = re.sub(r"\.[A-Za-z]+$", "", filename)
    stem = re.sub(r"\s*\(\d+\)$", "", stem)
    stem = re.sub(r"[\s_-]*-?\d[\d,]*\.\d{2}$", "", stem)
    return stem.strip(" _-") or stem


# --- generic auto-detection ---------------------------------------------------
# Rather than a hand-written template per carrier, try every plausible header
# band and keep the one whose table best reconciles against the statement's own
# totals. The arithmetic check doubles as the objective function, so a layout is
# only accepted when its own numbers agree.

TOTAL_RE = re.compile(r"\b(?:sub)?totals?\b|\bgrand\s+total\b|\btotal:", re.I)
MIN_CELLS = 3


# Page furniture that lands below the table and otherwise parses as a data row.
_FOOTER_RE = re.compile(
    r"page\s*\d+\s*of\s*\d+|print(ed)?\s*date|\S+@\S+\.\S+|"
    r"confidential|www\.|\d{3}[- ]\d{3}[- ]\d{4}",
    re.I,
)


def _looks_like_data(cells: dict[str, str]) -> bool:
    filled = [v for v in cells.values() if v]
    if len(filled) < MIN_CELLS:
        return False
    if _FOOTER_RE.search(" ".join(filled)):
        return False
    return any(to_float(v) is not None for v in filled)


def _looks_like_header(band: list[dict], rotated: bool) -> bool:
    texts = [_text(w, rotated) for w in band]
    if len(texts) < MIN_CELLS:
        return False
    alpha = [t for t in texts if re.search(r"[A-Za-z]{3}", t)]
    if len(alpha) < MIN_CELLS:
        return False
    # A header is labels, not figures.
    numeric = sum(1 for t in texts if to_float(t) is not None)
    return numeric <= len(texts) // 3


def _score(rows, totals, columns) -> tuple[int, int, list]:
    """(passing checks, row count, checks) for a candidate table."""
    checks = []
    for col in columns:
        stated = to_float(totals.get(col, ""))
        if stated is None:
            continue
        values = [to_float(r.get(col, "")) for r in rows]
        values = [v for v in values if v is not None]
        if values:
            checks.append(Check(col, round(sum(values), 2), stated))
    return sum(1 for c in checks if c.ok), len(rows), checks


def _auto_detect(pages) -> tuple | None:
    """Return the best (columns, rows, totals, subtotals, checks) or None."""
    best = None
    for lines, rotated in pages:
        bands = _bands(lines, rotated)
        for band in bands:
            if not _looks_like_header(band, rotated):
                continue
            names, extents = _columns(band, rotated)
            if len(names) < MIN_CELLS:
                continue
            band_end = max(_row_axis(w, rotated) for w in band)

            rows, totals_rows = [], []
            for line in lines:
                if min(_row_axis(w, rotated) for w in line) <= band_end:
                    continue
                cells = _cells(line, names, extents, rotated)
                joined = " ".join(cells.values())
                if TOTAL_RE.search(joined):
                    label = ""
                    for key, value in cells.items():
                        if TOTAL_RE.search(value):
                            label = label or value
                            cells[key] = ""
                    cells[names[0]] = label
                    totals_rows.append(cells)
                elif _looks_like_data(cells):
                    rows.append(cells)

            if not rows:
                continue
            totals = totals_rows[-1] if totals_rows else {}
            passing, nrows, checks = _score(rows, totals, names)
            # Prefer tables that reconcile; break ties on row count.
            key = (passing, nrows)
            if best is None or key > best[0]:
                best = (key, names, rows, totals, totals_rows[:-1], checks)

    if best is None:
        return None
    _, names, rows, totals, subs, checks = best
    return names, rows, totals, subs, checks


# --- failsafe: reconcile the exported amounts against the declared total ------
# The per-column Checks above compare rows to the totals row *inside* the table,
# which is self-consistent but cannot tell you the wrong table was extracted.
# This reconciles the commission figures that actually reach Excel against two
# references sourced independently of the table: the amount in the filename
# (the agency's own record of the payout) and a labelled total in the statement.

AMOUNT_RE = r"\(?-?\$?\s?[\d,]+\.\d{2}\)?"

# Ranked most to least trustworthy as "the final payout". Anything resembling a
# prior/opening balance, a payment already received, or a per-section subtotal is
# deliberately excluded - matching one of those would give a false pass.
DECLARED_TOTAL_LABELS: list[str] = [
    r"check\s*total",
    r"total\s*payout",
    r"net\s*amount",
    r"total\s*commission\s*paid(?:\s*to\s*agency)?",
    r"total\s*commission\s*amount",
    r"commissions?\s*total",
    r"total\s*disbursements",
    r"total\s*credit",
    r"commission\s*due\s*agency",
    r"commissions?\s*current\s*period",
    r"grand\s*total",
    r"total\s*amount\s*(?:due|paid)",
]

_EXCLUDE_LABEL = re.compile(
    r"prior|previous|opening|received|minimum|ytd|year\s*to\s*date|"
    r"written\s*premium|balance\s*forward|sub\s*total|subtotal",
    re.I,
)


@dataclass
class PayoutCheck:
    """Does the money we are about to export equal the money the statement says?"""

    exported_total: float | None = None
    commission_column: str = ""
    filename_total: float | None = None
    statement_total: float | None = None
    statement_label: str = ""

    @property
    def references(self) -> list[tuple[str, float]]:
        out = []
        if self.filename_total is not None:
            out.append(("filename", self.filename_total))
        if self.statement_total is not None:
            out.append((self.statement_label or "statement", self.statement_total))
        return out

    @property
    def references_disagree(self) -> bool:
        """The filename and the statement's own printed total differ."""
        if self.filename_total is None or self.statement_total is None:
            return False
        return abs(self.filename_total - self.statement_total) >= 0.01

    @property
    def status(self) -> str:
        """'match' | 'mismatch' | 'no_reference' | 'no_amounts'"""
        if self.exported_total is None:
            return "no_amounts"
        if not self.references:
            return "no_reference"
        if all(abs(self.exported_total - v) < 0.01 for _, v in self.references):
            return "match"
        return "mismatch"

    @property
    def ok(self) -> bool:
        return self.status == "match"

    def to_dict(self) -> dict:
        return {
            "exported_total": self.exported_total,
            "commission_column": self.commission_column,
            "filename_total": self.filename_total,
            "statement_total": self.statement_total,
            "statement_label": self.statement_label,
            "references": [{"source": s, "amount": v} for s, v in self.references],
            "references_disagree": self.references_disagree,
            "status": self.status,
            "ok": self.ok,
        }


def filename_amount(filename: str) -> float | None:
    """'Vertigo -214.80.pdf' -> -214.8. The agency's expected payout."""
    stem = re.sub(r"\.[A-Za-z]+$", "", filename)
    stem = re.sub(r"\s*\(\d+\)$", "", stem)
    m = re.search(r"(-?)\s*([\d,]+\.\d{2})\s*$", stem)
    if not m:
        return None
    value = float(m.group(2).replace(",", ""))
    return -value if m.group(1) else value


def declared_totals(page_lines) -> list[tuple[str, list[float]]]:
    """Labelled payout totals in the statement text, best-ranked first.

    Each entry is (label, amounts) because a totals line frequently carries more
    than one figure - SAIF's reads 'Producer commissions total 337.65 15.38',
    premium then commission. Taking the first amount would reconcile against
    premium and report a spurious mismatch, so all of them are returned and the
    caller picks.
    """
    texts = [
        " ".join(_text(w, rotated) for w in line)
        for lines, rotated in page_lines
        for line in lines
    ]
    found: list[tuple[str, list[float]]] = []
    for pattern in DECLARED_TOTAL_LABELS:
        for line in texts:
            for m in re.finditer(rf"({pattern})\s*:?\s*({AMOUNT_RE})", line, re.I):
                label = re.sub(r"\s+", " ", m.group(1)).strip()
                # Guard against 'PRIOR MONTH TOTAL ...' style prefixes.
                prefix = line[max(0, m.start() - 30) : m.start()]
                if _EXCLUDE_LABEL.search(prefix) or _EXCLUDE_LABEL.search(label):
                    continue
                amounts = [
                    v for v in (to_float(a) for a in re.findall(AMOUNT_RE, line[m.start(2):]))
                    if v is not None
                ]
                if amounts:
                    found.append((label, amounts))
    return found


def _payout_check(stmt: Statement, page_lines) -> PayoutCheck:
    check = PayoutCheck()
    check.filename_total = filename_amount(stmt.filename)

    candidates = declared_totals(page_lines)

    # Sum the column that actually reaches Excel. The commission column is
    # identified from the canonical mapping only - never by picking whichever
    # column happens to match a reference, which would make the check circular.
    mapping = canonical_map(stmt.template, stmt.columns)
    col = mapping.get("Commission Amount", "")
    if col and stmt.rows:
        values = [to_float(r.get(col, "")) for r in stmt.rows]
        values = [v for v in values if v is not None]
        if values:
            check.commission_column = col
            check.exported_total = round(sum(values), 2)

    # Pick which figure on a multi-amount totals line is the commission one. If
    # any candidate agrees with the filename, that is the corroborating pair;
    # otherwise fall back to the best-ranked label's last amount (commission is
    # conventionally the rightmost column). Note this only chooses which label to
    # cite - the filename stays an independent anchor, so the check is not
    # circular: a wrong export still fails against the filename.
    ref = check.filename_total
    for label, amounts in candidates:
        if ref is not None and any(abs(a - ref) < 0.01 for a in amounts):
            check.statement_label, check.statement_total = label, ref
            break
    else:
        if candidates:
            label, amounts = candidates[0]
            check.statement_label, check.statement_total = label, amounts[-1]
    return check


def _finalise(stmt: Statement, page_lines) -> None:
    """Run the payout failsafe and surface its verdict as a warning."""
    stmt.payout = _payout_check(stmt, page_lines)
    p = stmt.payout

    if p.status == "mismatch":
        refs = ", ".join(f"{s} says {v:,.2f}" for s, v in p.references)
        stmt.warnings.insert(
            0,
            f"FAILSAFE: the {p.commission_column} column exports "
            f"{p.exported_total:,.2f} but {refs}. Do not use this export until "
            f"the difference is explained.",
        )
        if p.references_disagree:
            stmt.warnings.insert(
                1,
                f"The two references also disagree with each other "
                f"({p.filename_total:,.2f} vs {p.statement_total:,.2f}), so the "
                f"filename may not reflect what this statement actually pays.",
            )
    elif p.status == "no_reference":
        stmt.warnings.append(
            "Failsafe could not run: no declared total found in the filename or "
            "the statement text to reconcile against."
        )
    elif p.status == "no_amounts" and stmt.rows:
        stmt.warnings.append(
            "Failsafe could not run: no commission column identified, so the "
            "exported amounts cannot be reconciled against the statement total."
        )
