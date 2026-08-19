"""Load and clean delimited statement exports (CSV / TSV).

Raw exports arrive with the formatting stripped out by whatever produced them.
Both files in statements/ are the same data, corrupted two different ways:

  agent_statement_gem_state.csv   dates as 20260728; amounts zero-padded but
                                  with the decimal point intact (000200.00)
  agent_statement_excel.csv       semicolon-delimited; dates and amounts already
                                  clean, but 'Commission %' holds 200 for 20.0%

The rules below are deliberately conservative. A column is rescaled only when the
evidence inside that column demands it, because dividing an already-correct
amount by 100 turns $200.00 into $2.00 and nothing downstream would notice.
Percent columns are never rescaled on a guess: their scale is resolved from the
amount/base columns, or the column is flagged and left alone.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass

from .extract import Statement, _finalise, carrier_from_filename, to_float

DELIMITERS = [",", ";", "\t", "|"]

DATE_HEADER_RE = re.compile(r"\bdate\b|\beff\b|\bexp(ir\w*)?\b", re.I)
MONEY_HEADER_RE = re.compile(
    r"amount|amt|premium|commission|comm\b|paid|pay\b|gross|net\b|balance|total|fee",
    re.I,
)
PERCENT_HEADER_RE = re.compile(r"%|percent|\brate\b", re.I)

# Non-transaction rows to drop. Gem State opens its export with a
# "CASH DISBURSED" line - a disbursement marker, not a policy transaction, so
# it inflates the premium total and is not a commission row.
#
# Matched on content rather than position. It is "normally" row 2, but a
# positional drop would silently delete a real commission row on any file that
# happens not to carry it.
DROP_ROW_MARKERS = ("CASH DISBURSED",)

YYYYMMDD_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
# 2026-07-28 / 2026/07/28, with any time component discarded
ISO_RE = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[ T].*)?$")
# 07/28/2026 or 7-28-26, with any time component discarded
MDY_RE = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})(?:[ T].*)?$")


@dataclass
class CleanupNote:
    column: str
    action: str
    detail: str

    def to_dict(self) -> dict:
        return {"column": self.column, "action": self.action, "detail": self.detail}


@dataclass
class ColumnPlan:
    name: str
    kind: str = "text"       # text | date | money | percent
    divisor: float = 1.0     # implied-decimal rescale; 1.0 means leave alone
    note: CleanupNote | None = None


def sniff(sample: str) -> str:
    """Pick the delimiter giving the most consistent column count."""
    best, best_score = ",", (-1.0, 0)
    lines = [ln for ln in sample.splitlines() if ln.strip()][:12]
    for d in DELIMITERS:
        counts = [ln.count(d) for ln in lines]
        if not counts or max(counts) == 0:
            continue
        mode = max(set(counts), key=counts.count)
        score = (counts.count(mode) / len(counts), max(counts))
        if score > best_score:
            best, best_score = d, score
    return best


def format_amount(value: float, decimals: int = 2) -> str:
    """1599.68 -> '1,599.68'. Dot decimal, comma thousands."""
    return f"{value:,.{decimals}f}"


def to_mdy(raw: str) -> str | None:
    """Normalise to mm/dd/yyyy, discarding any time. None if unrecognised."""
    s = (raw or "").strip()
    if not s:
        return ""
    for rx, order in ((YYYYMMDD_RE, "ymd"), (ISO_RE, "ymd"), (MDY_RE, "mdy")):
        m = rx.match(s)
        if not m:
            continue
        a, b, c = m.group(1), m.group(2), m.group(3)
        if order == "ymd":
            year, month, day = int(a), int(b), int(c)
        else:
            month, day, year = int(a), int(b), int(c)
            if year < 100:
                year += 2000 if year < 70 else 1900
        if not (1 <= month <= 12 and 1 <= day <= 31 and 1900 <= year <= 2200):
            return None
        return f"{month:02d}/{day:02d}/{year:04d}"
    return None


def _plan_column(name: str, values: list[str]) -> ColumnPlan:
    """Classify a column and decide whether it needs rescaling."""
    filled = [v.strip() for v in values if v.strip()]
    if not filled:
        return ColumnPlan(name)

    looks_dated = DATE_HEADER_RE.search(name) or all(
        YYYYMMDD_RE.match(v) for v in filled
    )
    if looks_dated:
        parsed = [to_mdy(v) for v in filled]
        if all(p not in (None, "") for p in parsed):
            plan = ColumnPlan(name, "date")
            # Guard against a dd/mm source being silently read as mm/dd.
            firsts = [int(m.group(1)) for v in filled if (m := MDY_RE.match(v))]
            if firsts and max(firsts) > 12:
                plan.kind = "text"
                plan.note = CleanupNote(
                    name,
                    "flagged",
                    "The first component exceeds 12 on some rows, so this looks "
                    "like dd/mm/yyyy rather than mm/dd/yyyy. Left unchanged - "
                    "confirm the source order before converting.",
                )
            return plan

    numeric = [to_float(v) for v in filled]
    if any(n is None for n in numeric):
        return ColumnPlan(name)

    is_percent = bool(PERCENT_HEADER_RE.search(name))
    is_money = bool(MONEY_HEADER_RE.search(name)) and not is_percent
    if not (is_percent or is_money):
        return ColumnPlan(name)

    has_decimal_point = any("." in v for v in filled)
    all_integers = all(re.fullmatch(r"-?\(?\d+\)?", v) for v in filled)

    if is_percent:
        plan = ColumnPlan(name, "percent")
        if not has_decimal_point and all_integers:
            plan.note = CleanupNote(
                name,
                "needs-confirmation",
                "Integer percents with no decimal point - the scale is ambiguous "
                "(200 could mean 20.0% or 2.00%).",
            )
        return plan

    plan = ColumnPlan(name, "money")
    if not has_decimal_point and all_integers:
        plan.divisor = 100.0
        plan.note = CleanupNote(
            name,
            "rescaled",
            "No decimal point and every value an integer, so the last two digits "
            "were treated as cents (divided by 100).",
        )
    elif has_decimal_point:
        plan.note = CleanupNote(
            name,
            "reformatted",
            "Decimal point already present: leading zeros stripped and thousands "
            "separators added. Values were NOT rescaled.",
        )
    return plan


def _resolve_percent_scale(raw_rows, pct_col, amount_col, base_col):
    """Infer a percent column's scale from amount/base instead of guessing."""
    ratios = []
    for row in raw_rows:
        amt = to_float(row.get(amount_col, ""))
        base = to_float(row.get(base_col, ""))
        pct = to_float(row.get(pct_col, ""))
        if None in (amt, base, pct) or not base or not pct:
            continue
        ratios.append((amt / base * 100) / pct)
    if not ratios:
        return None, "No rows had both an amount and a base to check against."
    mean = sum(ratios) / len(ratios)
    for candidate in (1.0, 10.0, 100.0, 1000.0):
        if abs(mean - 1 / candidate) < 0.02:
            return candidate, (
                f"Scale confirmed against {amount_col} / {base_col} over "
                f"{len(ratios)} rows: divided by {candidate:g} "
                f"(200 means {200 / candidate:g}%)."
            )
    return None, f"Could not confirm a scale (mean ratio {mean:.4f})."


def _drop_marker(row: dict[str, str]) -> str | None:
    """Return the marker text if this row is a non-transaction line to drop."""
    for value in row.values():
        collapsed = re.sub(r"\s+", " ", (value or "").strip()).upper()
        if collapsed in DROP_ROW_MARKERS:
            return collapsed
    return None


def _clean_value(raw: str, plan: ColumnPlan) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    if plan.kind == "date":
        return to_mdy(s) or s
    if plan.kind in ("money", "percent"):
        value = to_float(s)
        if value is None:
            return s
        value /= plan.divisor
        decimals = 2 if plan.kind == "money" else 1
        return format_amount(value, decimals)
    return s


def parse_delimited(path: str, filename: str) -> Statement:
    """Read a CSV/TSV export into the same Statement shape the PDFs produce."""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        raw = fh.read()

    stmt = Statement(filename=filename)
    stmt.template = f"{carrier_from_filename(filename)} (delimited export)"

    if not raw.strip():
        stmt.warnings.append("The file is empty.")
        return stmt

    delimiter = sniff(raw)
    rows = [
        r
        for r in csv.reader(io.StringIO(raw), delimiter=delimiter)
        if any(c.strip() for c in r)
    ]
    if len(rows) < 2:
        stmt.warnings.append("No data rows found below the header.")
        return stmt

    header = [h.strip() for h in rows[0]]
    while header and not header[-1]:  # trailing delimiter artifact
        header.pop()
    width = len(header)
    body = [(r + [""] * width)[:width] for r in rows[1:]]

    named = [(i, h) for i, h in enumerate(header) if h]
    plans = {h: _plan_column(h, [r[i] for r in body]) for i, h in named}

    # Resolve any ambiguous percent scale from the data rather than guessing.
    money_cols = [h for h, p in plans.items() if p.kind == "money"]
    ambiguous = [
        h
        for h, p in plans.items()
        if p.kind == "percent" and p.note and p.note.action == "needs-confirmation"
    ]
    if ambiguous and len(money_cols) >= 2:
        amount_col = next(
            (c for c in money_cols if re.search(r"comm", c, re.I)), money_cols[0]
        )
        base_col = next((c for c in money_cols if c != amount_col), money_cols[-1])
        raw_rows = [{h: r[i] for i, h in named} for r in body]
        for pct in ambiguous:
            divisor, detail = _resolve_percent_scale(
                raw_rows, pct, amount_col, base_col
            )
            if divisor and divisor != 1.0:
                plans[pct].divisor = divisor
                plans[pct].note = CleanupNote(pct, "rescaled", detail)
            elif divisor:
                plans[pct].note = CleanupNote(pct, "verified", detail)
            else:
                plans[pct].note = CleanupNote(
                    pct, "flagged", detail + " Left unchanged."
                )

    stmt.columns = [h for _, h in named]
    dropped: list[str] = []
    for r in body:
        row = {h: _clean_value(r[i], plans[h]) for i, h in named}
        if not any(row.values()):
            continue
        marker = _drop_marker(row)
        if marker:
            dropped.append(marker)
            continue
        stmt.rows.append(row)

    stmt.cleanup = [p.note.to_dict() for p in plans.values() if p.note]
    if dropped:
        seen = ", ".join(sorted(set(dropped)))
        stmt.cleanup.append(
            CleanupNote(
                seen,
                "dropped",
                f"Removed {len(dropped)} non-transaction row"
                f"{'' if len(dropped) == 1 else 's'} matched by content, not by "
                f"position. This is a disbursement marker rather than a policy "
                f"transaction, so it would otherwise inflate the premium total.",
            ).to_dict()
        )
    stmt.delimiter = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}[
        delimiter
    ]

    # No page text for a CSV, so the failsafe reconciles against the filename.
    _finalise(stmt, [])
    return stmt
