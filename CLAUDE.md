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

OCR additionally needs the Tesseract engine, which pip cannot install:

```bash
winget install UB-Mannheim.TesseractOCR
```

Accounts are created from the command line — there is no signup form:

```bash
python scripts/adduser.py add you@fignow.com --name "You" --admin
python scripts/adduser.py list
```

There is **no test suite**. Verification is done by three report scripts that
sweep every file in `statements/` and print measured results — treat these as
the regression check after touching any parsing code:

```bash
python scripts/carriers_report.py    # regenerates CARRIERS.md; per-carrier status
python scripts/failsafe_report.py    # per-file payout reconciliation
python scripts/ocr_report.py         # OCR settings vs the image-only scans
```

`ocr_report.py` takes several minutes — it re-OCRs every scan once per candidate
setting. Run it only when changing something in `app/ocr.py`.

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
        ↳ app/ocr.py    (only when a page has no text layer: raster → word boxes)
CSV  → app/tabular.py   (delimiter sniff → per-column cleaning plan)
                    ↓
              Statement  ── _finalise() → PayoutCheck (the failsafe)
                    ↓
              app/store.py (SQLite: statements, users, sessions)
                    ↓
        app/main.py (REST, behind app/auth.py) → app/static (UI) · app/excel.py
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

### The OCR tier (`app/ocr.py`)

Used only when a page has no text layer. It produces the **same word dicts**
`page.extract_words()` returns, so the geometry engine, totals detection and
failsafe all run on a scan unchanged. It deliberately does not try to understand
tables — that is already the engine's job.

Three things it must keep doing:

1. **Return points, not pixels.** `LINE_TOL`, `BAND_GAP` and `MERGE_GAP` are
   measured in PDF points. Pixel coordinates at 300 DPI would inflate every gap
   by 4.167x and break all three silently.
2. **Measure orientation rather than trust OSD.** Many scans are sideways
   (Heacock is a portrait page holding landscape content). Tesseract's OSD gave
   13.83% confidence on a page it read correctly, so it is used as a hint and the
   rotation that yields the most confident words wins.
3. **Use `--psm 6`, not Tesseract's default 3.** The scans carry vertical fold
   lines, and PSM 3's layout analysis reads them as column boundaries and
   discards whole regions — on Heacock it missed both `$16.44` figures in the
   right-hand column. Measured 12/21 against 3's 10/21 with the filename amount
   as ground truth (`scripts/ocr_report.py`).

**OCR misreads digits, so the failsafe is what makes this safe to ship.** Four
carriers currently extract figures that do not reconcile and are blocked from
export. Never loosen the failsafe to make a scan pass.

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

### Auth and persistence

`app/auth.py` installs **one middleware** in front of everything. It is not a
per-route dependency, because `main.py` mounts the frontend as a catch-all at
`/` — a route-by-route guard would leave `index.html` and `app.js` public, so the
UI would load and only the JSON would 401. That looks protected without being
protected. `PUBLIC_PATHS` is therefore the complete anonymous surface.

Browsers get a 303 to `/login`; `/api/*` gets 401 JSON, so `api()` in `app.js`
can redirect on an expired session instead of rendering an HTML page into a
`fetch`.

Sessions are **rows in SQLite, not signed cookies** — a signed cookie cannot be
revoked, so logout and disabling a user would both keep working until expiry.
Passwords use stdlib `hashlib.scrypt` (~0.11s per verify), keeping the dependency
count unchanged. There is **no signup route**; accounts come from
`scripts/adduser.py`.

`Secure` on the cookie is derived from the request scheme, not hardcoded: over
Tailscale this is real HTTPS and the flag must be set, but a Secure cookie is
never returned over `http://localhost` and local development could not log in.

**Export scoping matters more than it looks.** In memory, "export all" meant
today's uploads. Once statements persist it would mean *every statement ever
uploaded by anyone*, so exports take explicit `?ids=`, and the UI sends its
working set. An absent `ids=` falls back to the caller's last 24 hours.

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
- Statements are **persisted in SQLite** (`app/store.py`, `data/statements.db`),
  with the source file in `data/uploads/`. `data/` is gitignored — it holds the
  same client PII as `statements/`, plus password hashes.
- Static assets are served `no-store` because a cached `index.html` against a
  fresh `app.js` silently breaks the UI.

## Known issues

- **Totals-row leak (fixed).** `split_trailing_total()` in `app/extract.py` now
  catches totals rows that `TOTAL_RE` cannot see because they carry no "total"
  keyword — Grundy-Phly and FARMERS-ALLIANCE close their tables with a producer
  subtotal labelled by the agency's own code and name. Detection is **arithmetic**:
  the row is a total only if every figure it carries equals the column sum of the
  rows above, with at least two columns agreeing and the row sparser than those
  above it (that guard is what protects the single-data-row case). `tabular.py`
  gained `_split_totals_row()`, which tries the labelled case first — keyword
  *plus* the sparseness guard, because "Total Comfort Heating" is a plausible
  insured name — then falls back to the arithmetic test.
- **`FARMERS-ALLIANCE 124.23.pdf` is misnamed** and still reports a failsafe
  mismatch as a result. The statement declares `Commission Due Agency 124.03` and
  its two transactions sum to exactly that; the filename's `124.23` transposes two
  digits. Renaming the file to `124.03` clears it. This is a data-entry error the
  failsafe caught, not a parser bug — do not "fix" it in code.
- **Auto-detector can pick a non-table band (improved, not fixed).** Band
  scoring now includes two fit measures beyond `passing` and row count: `fused`
  (cells that swallowed two or more figures — the decisive one) and `alignment`
  (words landing inside any column). This fixed Polomar and Safehold and is what
  makes the OCR reads usable. **United Life still parses 27 rows from its address
  block, and TransAmerica still grabs letterhead.** Note alignment cannot lead the
  ranking: a band with few wide columns scores *higher* on it than the real header
  (Heacock's letterhead 0.88 vs the true header's 0.83), so ranking it first picks
  the letterhead. Row count must also outrank `fused`, or a stray data line near
  the page foot wins on having fewer lines beneath it (ALLIED-BENEFIT-BEAM).
- **OCR reads 20 of the 21 scans; 4 produce figures that fail the failsafe.**
  Burns & Wilcox (12,409.20 vs 597.90), CNA (30.60 vs 110.10), Flathead
  (42,823.96 vs 937.91) and TransAmerica (856.00 vs 233.37) extract a table whose
  commission column does not reconcile. Export is blocked, which is the failsafe
  working as designed — OCR misreads digits and these are the cases it caught.
  Improving them means better pre-processing (the scans carry fold lines) or a
  hand-written template, **not** loosening the check.
- **`ISC 58.65.pdf` reports 0 pages at 788KB** and cannot be opened by pdfplumber
  at all. This is a corrupt file, not an OCR case; it needs re-exporting from the
  carrier.
