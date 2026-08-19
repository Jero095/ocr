# Features

Every capability the app currently has. Status is **measured** — the figures
here come from `scripts/carriers_report.py` and `scripts/failsafe_report.py`
sweeping every file in `statements/`, not from intent.

Current coverage: **46 carriers / 57 files** — 14 verified, 14 unverified,
2 with no locatable table, 16 blocked on OCR.

---

## 1. Ingestion

### PDF statements
- **Digital text-layer extraction.** No OCR involved; the text layer is read
  directly, so figures are exact rather than recognised.
- **Rotated-page handling.** Statements stored with a 180° page rotation (every
  string held in reversed character order, reading order running down the page)
  are un-rotated and re-columned. These are the ones that produce scrambled
  gibberish on copy/paste in any PDF viewer — the original motivation for the app.
- **Per-page rotation.** A statement may mix a rotated table page with an
  unrotated cover or address page; each page is handled on its own terms.
- **Multi-page tables.** The table is located wherever it is (Vertigo's is on
  page 2).
- **Wrapped multi-line headers.** A header split across five lines
  (Vertigo: `Policy` / `Effective` / `Date`) is reassembled into one column name.

### Delimited exports (CSV / TSV / TXT)
- **Delimiter sniffing** across `,` `;` `\t` `|`, chosen by column-count
  consistency. Gem State ships the same data comma- *and* semicolon-delimited.
- **Cleanup reporting** — every column touched is listed with what changed and
  why, tagged `rescaled` / `reformatted` / `verified` / `flagged` / `dropped`.

### Rejected input
Anything that is not PDF/CSV/TSV/TXT is refused with a 400. Image-only PDFs are
accepted but report clearly that they need OCR rather than silently yielding
nothing.

---

## 2. Layout recognition

### Hand-written templates
A `Template` declares only what is carrier-specific: header tokens, the row
pattern, the canonical column mapping, and an optional `base × rate == result`
check. Geometry is shared. Currently: Chris Leef, Vertigo.

### Self-validating auto-detection
For unknown carriers, every plausible header band is tried and the one whose
table best reconciles against the statement's own printed totals is kept — the
arithmetic check doubles as the layout chooser. This covers ~12 further carriers
with no per-carrier code.

### False-positive recovery
Header tokens are not unique across carriers (Amwins' header contains every
token in the Chris Leef template). A template that matches but yields no rows
falls through to auto-detection rather than reporting an empty table.

### Carrier naming from filename
`Big Sky 362.10.PDF` → `Big Sky`; handles `_`-separated names, `(1)` duplicate
suffixes, and negative amounts (`Vertigo -214.80.pdf`).

---

## 3. Data cleaning (delimited sources)

### Dates → `mm/dd/yyyy`, no time
Accepts `20260728`, `2026-07-28`, `2026/07/28 14:30:00`, `07/28/2026 09:15`,
`7-28-26`. Invalid values (`20261332`) are left untouched rather than coerced.

**Guard:** if the first component exceeds 12 on any row, the column is likely
`dd/mm/yyyy`; it is flagged and left unchanged rather than silently mis-converted.

### Amounts → `.` decimal, `,` thousands
`0001599.68` → `1,599.68`. Negatives and accounting-style parentheses preserved.

**Implied decimals** (last two digits are cents) are applied **only** when a
column has no decimal point anywhere *and* every value is an integer. Gem State's
amounts already carry decimals — rescaling them would turn `$200.00` into `$2.00`
with nothing downstream to catch it.

### Percent scale resolved, never guessed
`Commission %` arriving as `200` for 20.0% is ambiguous (÷10? ÷100?). The scale
is derived from `Commission Amount / Premium Amount × 100` across all rows —
which confirmed ÷10 — or the column is flagged and left alone.

### Non-transaction row removal
`DROP_ROW_MARKERS` removes lines that are not policy transactions (Gem State's
`CASH DISBURSED`, which inflates the premium total). Matched by **content, not
row position** — a positional drop would delete a real commission row on any
file lacking that line. Exact whole-cell match, case- and whitespace-insensitive,
so `CASH DISBURSED TO AGENT` is deliberately left alone. Every removal is
reported.

---

## 4. Validation

Two independent layers. The distinction matters: the first proves the table is
internally consistent, the second proves it is the *right* table.

### Per-column checks (internal)
For every column where the totals row holds a number, row values are summed and
compared. Numeric columns are detected per layout rather than hardcoded. Plus a
per-row `base × rate == result` check where the template declares one, using an
absolute amount tolerance (not percentage points, which would false-alarm on
cent-level rounding).

### The payout failsafe (independent)
Sums the commission column **that actually reaches Excel** and reconciles it
against references sourced outside the table:

1. **The amount in the filename** — present on all 57 files.
2. **A labelled total in the statement text** — `Check Total`, `Total Payout`,
   `NetAmount`, `Commissions Total`, etc., ranked so prior balances, payments
   received and section subtotals cannot produce a false pass.

Verdicts: `match` · `mismatch` · `no_reference` · `no_amounts`. Current results:
**PASS 13 · FAIL 2 · could not run 31** (per carrier).

Design constraints:
- The commission column is identified from the canonical mapping **only** —
  never by picking whichever column matches a reference, which would make the
  check circular.
- Payment columns outrank commission-earned columns, because the declared total
  is what was *paid* (US Risk earns 1,696.15 but pays 961.02).
- Multi-amount totals lines are handled: a line reading
  `Producer commissions total 337.65 15.38` is premium then commission, so the
  corroborating figure is chosen rather than the first.
- If the filename and the statement's own total disagree with each other, that
  is reported separately — it may mean the filename does not reflect what the
  statement pays.

### Enforcement
A `mismatch` **disables Copy / Excel / TSV in the UI and returns 409** from every
export endpoint. Unreconciled figures cannot leave the app through any path,
including direct API access.

---

## 5. Export

### Excel (`.xlsx`)
- One sheet per statement, faithful to that carrier's own columns.
- **`All Rows`** sheet mapping every carrier onto a shared schema — necessary
  because carriers share no column names.
- **`Validation`** sheet led by the failsafe verdict, then per-column checks and
  warnings, colour-coded.
- Amounts are **real numbers** with `#,##0.00`; percentages stored as fractions
  (`5.00%` → `0.05`); `(75.00)` becomes `-75`. Everything is summable in Excel.
- An unambiguous 4-digit-year `mm/dd/yyyy` becomes a **real Excel date**
  (sortable). Two-digit years stay text rather than guessing the century.
  Policy numbers stay text.
- Frozen header, autofilter, auto-sized columns, bold totals, italic subtotals.

### TSV
Per-statement (the carrier's own columns) and combined across statements (the
shared schema). Pastes straight into Excel or Sheets.

### Clipboard
Click any cell to copy it; click a column header to copy the whole column;
"Copy table" for the full TSV. Falls back to a hidden textarea where the
Clipboard API is unavailable (non-secure context).

---

## 6. Interface

- Drag-and-drop or browse; multiple files at once.
- Sidebar listing every loaded statement with a green/red status dot and row count.
- Carrier line showing which layout was recognised and how.
- **Failsafe banner** above the table — green pass / red fail / amber
  could-not-run, with the exported figure and every reference it was checked
  against.
- **Cleanup panel** (delimited sources) listing each column touched and why.
- **Needs-review panel** for warnings.
- Side-by-side source PDF view.
- Dark/light theme, persisted.
- Responsive down to a single column.

---

## 7. Reporting

- **`CARRIERS.md`** — generated registry of every carrier with status, failsafe
  verdict, exported vs declared amounts, row counts and how it was parsed.
  Regenerate with `python scripts/carriers_report.py`.
- **`scripts/failsafe_report.py`** — per-file reconciliation table, flagging
  where the two references disagree with each other.

---

## Not implemented

- **OCR.** 16 carriers are image-only scans and cannot be read at all. This is
  the single largest coverage unlock.
- **Persistence.** Statements live in memory; a restart clears them.
- **Authentication.** There is none — do not expose the app publicly as-is.
- **LLM fallback** for layouts the auto-detector cannot crack.
