# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local web app that extracts commission-statement tables from insurance-carrier
PDFs and CSV/TSV exports, validates the figures against the statement's own
declared totals, and exports to Excel/TSV. Built for copying values out — the
source PDFs are frequently unusable via copy/paste.

Companion docs: [docs/FEATURES.md](docs/FEATURES.md) ·
[docs/STACK.md](docs/STACK.md) · [docs/FILES.md](docs/FILES.md) ·
[CARRIERS.md](CARRIERS.md) (per-carrier support status)

## Commands

```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000   # serves UI + API on :8000
```

There is **no test suite**. Verification is done by two report scripts that
sweep every file in `statements/` and print measured results — treat these as
the regression check after touching any parsing code:

```bash
python scripts/carriers_report.py    # regenerates CARRIERS.md; per-carrier status
python scripts/failsafe_report.py    # per-file payout reconciliation
```

To check a single file while iterating:

```bash
python -c "from app.extract import parse_pdf; s=parse_pdf('statements/X.pdf','X.pdf'); print(s.to_tsv()); print(s.payout.status, s.warnings)"
```

`statements/` holds real client data and is gitignored, as is anything extracted
from it (`*.csv`, `*.tsv`, `*.xls[x]`).

## Architecture

Two ingestion paths converge on one `Statement` dataclass
(`app/extract.py`), which is what the API, Excel writer and UI all consume.
Adding a third source type means producing a `Statement`, nothing else.

```
PDF  → app/extract.py   (geometry engine → template registry / auto-detector)
CSV  → app/tabular.py   (delimiter sniff → per-column cleaning plan)
                    ↓
              Statement  ── _finalise() → PayoutCheck (the failsafe)
                    ↓
        app/main.py (REST) → app/static (UI) · app/excel.py (.xlsx)
```

### The PDF geometry engine

Carriers share no layout, so geometry is generic and only per-carrier specifics
live in `TEMPLATES`. Three things make one engine serve wildly different pages:

1. **Axis normalisation.** A 180°-rotated page stores strings in reversed
   character order with visual left-to-right running along *descending* `top`.
   `_span()` negates to `(-bottom, -top)` so all downstream comparisons are
   plain ascending interval maths. Rotation is tracked **per line**, because a
   statement can mix a rotated table page with an unrotated address page.
2. **Header bands.** Consecutive lines within `BAND_GAP` merge into one band,
   which makes wrapped multi-line headers work for free.
3. **Overlap assignment.** `_cells()` assigns each word to the column it
   overlaps most — never by distance-merging words into runs. A word overlapping
   *no* column attaches to the previous cell (the wrapped-text case).

**Do not "simplify" step 3 into a distance-based merge.** It was tried and
fails: on the Chris Leef totals line, adjacent figures in different columns sit
0.5pt apart, so any distance merge fuses three columns into one.

### Two ways a layout is recognised

- `TEMPLATES` — hand-written entries (currently Chris Leef, Vertigo). Header
  tokens are **not unique across carriers**: Amwins' header contains every token
  in the Chris Leef template. A template that matches but yields no rows
  therefore falls through to auto-detection.
- `_auto_detect()` — tries every plausible header band and keeps whichever table
  best reconciles against the statement's own totals. The arithmetic check *is*
  the layout chooser. This covers most carriers with no per-carrier code.

### The failsafe (`_payout_check`)

Per-column `Check`s compare rows to the totals row *inside* the table —
self-consistent, but blind to having extracted the wrong table. The failsafe is
independent: it sums the commission column that actually reaches Excel and
reconciles it against references sourced **outside** the table (the amount in
the filename, plus any labelled total in the statement text, ranked so prior
balances and section subtotals cannot produce a false pass).

A `mismatch` disables export in the UI **and** returns 409 from every export
endpoint (`_guard_failsafe`). Unreconciled figures must not leave the app.

The commission column is identified from `canonical_map` only — **never** by
picking whichever column happens to match a reference, which would make the
check circular and worthless.

### Cross-carrier export

Carriers share no column names, so `CANONICAL_FIELDS` + `canonical_map()`
bridge them for the combined `All Rows` sheet and `/api/export.tsv`.
Hand-written templates declare the mapping; auto-detected layouts have it
*inferred* by keyword (`infer_canonical`). Inference has been wrong before
(Amwins' `Agency Gross` is a base, not a commission) — the failsafe caught it.

## Conventions that matter here

- **Constants are measured, not chosen.** `BAND_GAP = 8` sits between the widest
  intra-header line gap (5.0) and the narrowest header-to-data gap (11.1).
  `MERGE_GAP = 4.0` sits between 2.2 and 5.3 — the tightest margin in the
  codebase. If you change either, re-run both report scripts and quote numbers.
- **Never widen a pattern to force a match.** A wrongly-identified commission
  column produces a false PASS, which is worse than reporting that the failsafe
  could not run. Prefer flagging over guessing throughout.
- **Never rescale a number without evidence in the data.** `app/tabular.py`
  applies the implied-decimal rule only when a column has no decimal point
  anywhere *and* every value is an integer — dividing an already-correct
  `000200.00` by 100 gives `$2.00` and nothing downstream would notice. Percent
  scale is resolved from `amount / base`, not assumed.
- **Content matching over positional matching** for row filtering
  (`DROP_ROW_MARKERS`). A positional drop deletes real data on any file that
  does not carry the expected line.
- **Dates:** only an unambiguous 4-digit-year `mm/dd/yyyy` becomes a real Excel
  date. Two-digit years stay text rather than guessing the century.
- Statements are held **in memory** (`STATEMENTS` in `app/main.py`); a restart
  clears them. Static assets are served `no-store` because a cached
  `index.html` against a fresh `app.js` silently breaks the UI.

## Known issues

- **Totals-row leak (open).** Three files double-count because a totals or
  subtotal row passes the data-row test: `FARMERS-ALLIANCE`, `Grundy-Phly`, and
  any CSV/TSV containing a totals row — `app/tabular.py` has **no totals-row
  detection at all**, unlike the PDF path's `TOTAL_RE`. One fix clears all three.
- **Auto-detector can pick a non-table band.** United Life parses 27 rows from
  the address block; TransAmerica and Guard grab letterhead. They report amber
  and export nothing, but present as "parsed N rows". `_looks_like_header` should
  require alignment with the rows beneath it.
- **16 carriers are image-only scans** with no text layer and need an OCR tier.
  `ISC 58.65.pdf` also reports 0 pages and is likely corrupt.
