# Files

What each file is responsible for, and the load-bearing pieces inside it.

```
CLAUDE.md                     guidance for Claude Code
CARRIERS.md                   generated per-carrier support registry
requirements.txt              6 runtime deps + the Tesseract engine
.gitignore                    excludes client data
.claude/launch.json           dev-server config for the Browser pane
docs/
  FEATURES.md                 every capability
  STACK.md                    technology choices and rejected alternatives
  FILES.md                    this file
  TAILSCALE.md                serving and sharing over Tailscale (es)
app/
  __init__.py                 marks app/ a package (empty)
  extract.py     971 lines    PDF geometry engine, templates, failsafe, Statement
  ocr.py         226 lines    OCR for image-only scans -> word boxes in points
  tabular.py     383 lines    CSV/TSV loading and cleaning
  store.py       533 lines    SQLite: statements, users, sessions
  auth.py         96 lines    the gate: middleware, session cookies
  excel.py       247 lines    .xlsx workbook construction
  main.py        350 lines    FastAPI routes, upload, export guard
  static/
    app.js       462 lines    all frontend behaviour
    style.css    557 lines    design tokens, dark/light, layout
    index.html    79 lines    page skeleton
    login.html    73 lines    sign-in page
scripts/
  carriers_report.py  194     sweeps statements/, regenerates CARRIERS.md
  failsafe_report.py   37     per-file payout reconciliation table
  ocr_report.py       147     OCR settings swept against the scans
statements/                   real client statements - gitignored
```

---

## `app/extract.py` — the core

The largest and most intricate file. Owns the shared `Statement` model, so both
ingestion paths and every consumer depend on it.

**Data model**
- `Statement` — the single shape everything downstream consumes: `columns`,
  `rows`, `totals`, `subtotals`, `checks`, `payout`, `warnings`, plus
  `cleanup`/`delimiter` used only by delimited sources. `to_dict()` feeds the
  API; `to_tsv()` feeds clipboard and TSV export.
- `Check` — one per-column row-sum vs stated-total comparison.
- `PayoutCheck` — the failsafe verdict (`match` / `mismatch` / `no_reference` /
  `no_amounts`), plus `references_disagree` when the filename and the statement's
  own total contradict each other.
- `Template` — per-carrier config only: header tokens, row pattern, canonical
  mapping, optional rate check. Geometry is never per-carrier.

**Geometry** (`_row_axis`, `_span`, `_text`, `_lines`, `_bands`, `_columns`, `_cells`)
- `_span()` negates to `(-bottom, -top)` on rotated pages so ascending order
  always means visual left-to-right. This is why one engine handles both
  orientations.
- `_bands()` merges lines within `BAND_GAP` — how wrapped headers work.
- `_cells()` assigns words by **maximum interval overlap**, with no-overlap words
  attaching to the previous cell. Read the docstring before changing it; the
  distance-merge alternative was tried and breaks the Chris Leef totals line.

**Recognition**
- `TEMPLATES` — hand-written entries.
- `_auto_detect()` — tries every plausible header band, keeps the one that best
  reconciles against the statement's own totals.
- `_looks_like_header` / `_looks_like_data` — the gates. `_FOOTER_RE` strips page
  furniture (`Page 1 of 1`, print dates, emails) that otherwise parses as data.
- `carrier_from_filename()` — strips amounts and `(1)` suffixes.

**Canonical mapping** — `CANONICAL_FIELDS`, `_CANONICAL_PATTERNS`,
`infer_canonical()`, `canonical_map()`. Lives here rather than in `excel.py`
because the failsafe needs to know which column is the commission column.
Payment patterns are ordered ahead of commission-earned patterns.

**Failsafe** — `filename_amount()`, `declared_totals()`, `_payout_check()`,
`_finalise()`. `_finalise()` is called on every `parse_pdf` exit path *and* by
`tabular.py`, so the failsafe runs for every source type.

**Tunables** — `LINE_TOL`, `BAND_GAP`, `MERGE_GAP`, `MIN_CELLS`. All measured;
each carries the measurement in a comment.

---

## `app/ocr.py` — the OCR tier

Called from `parse_pdf` only when a page yields no words. Produces the same word
dicts as `extract_words()`, so nothing downstream changes.

- `available()` — (usable, reason). Reason is surfaced to the UI, so a missing
  Tesseract install explains itself instead of looking like a parse failure.
- `_find_tesseract()` — PATH, then `TESSERACT_CMD`, then the standard Windows
  install paths (the installer does not always reach an open shell's PATH).
- `page_words()` — rasterise, choose a rotation, OCR, scale pixels to points.
  Returns a note describing DPI, rotation, word count and mean confidence, which
  lands in `stmt.warnings` so the reader knows the figures came from OCR.
- `_words()` — converts one `image_to_data` result, returning mean confidence and
  a discard count so rotations can be compared on measurement.
- Constants: `RENDER_DPI`, `TESSERACT_CONFIG` (`--psm 6`), `MIN_CONF`. All
  measured via `scripts/ocr_report.py`; the rationale is in the module docstring.

## `app/tabular.py` — delimited exports

Produces the same `Statement`, so everything downstream is unchanged.

- `sniff()` — delimiter by column-count consistency across `,` `;` `\t` `|`.
- `to_mdy()` — normalises `20260728`, `2026-07-28`, `07/28/2026 09:15`, `7-28-26`
  to `mm/dd/yyyy`, discarding time; returns `None` on anything unrecognised.
- `_plan_column()` — classifies each column (`text` / `date` / `money` /
  `percent`) and decides whether it needs rescaling. This is where the
  conservative rules live: implied decimals only when there is no decimal point
  anywhere *and* all values are integers; a `dd/mm` guard when a first component
  exceeds 12.
- `_resolve_percent_scale()` — derives an ambiguous percent scale from
  `amount / base` rather than assuming ÷100.
- `_drop_marker()` / `DROP_ROW_MARKERS` — removes non-transaction rows by
  content, not position.
- `CleanupNote` — every change is reported to the UI rather than applied silently.
- `_split_totals_row()` — moves a trailing totals row out of the data rows, so a
  CSV that closes with one no longer double-counts. Tries the labelled case first
  (keyword *and* sparser than the rows above — a policyholder called "Total
  Comfort Heating" must not be discarded), then delegates to
  `split_trailing_total()` for unlabelled ones. Also populates `stmt.totals`,
  which gives delimited sources the per-column checks they previously never got.

---

## `app/store.py` — persistence

SQLite via stdlib `sqlite3`, no ORM: `Statement.to_dict()` is already JSON, so a
statement is one payload blob plus indexed columns for listing.

- `connect()` — sets WAL (a long history read must not block an upload) and
  `foreign_keys=ON`.
- `hash_password` / `verify_user` — stdlib `hashlib.scrypt`. A missing account
  still pays the hash cost, so timing cannot enumerate who has an account.
- `create_session` / `session_user` / `delete_session` — server-side sessions, so
  logout and disabling a user take effect immediately.
- `lock_remaining` / `_record_failure` — per-email lockout. Per-email rather than
  per-IP because everyone arrives over the same Tailscale connection.
- `save_statement` / `get_statement` / `list_statements` — the payload is the
  source of truth; the scalar columns are denormalised copies for filtering.
- `recent_ids()` — the default export scope.
- `duplicate_of()` — same file uploaded twice, by content hash. Surfaced, not
  blocked; history is shared so a second upload may be deliberate.

## `app/auth.py` — the gate

One middleware, not per-route dependencies — see the CLAUDE.md rationale.
`PUBLIC_PATHS` is the complete anonymous surface. `set_session_cookie()` derives
`Secure` from the request scheme so HTTPS is enforced over Tailscale without
breaking `http://localhost`.

## `app/excel.py` — workbook construction

- `build_workbook()` — assembles `All Rows` (cross-carrier), one sheet per
  statement, and `Validation` (failsafe first, then per-column checks and
  warnings).
- `_write()` — the typing boundary. Decides real date vs percent-as-fraction vs
  money vs text. `STRICT_MDY_RE` only accepts 4-digit years; anything less
  certain stays text.
- `_statement_sheet` / `_all_rows_sheet` / `_checks_sheet` — per-sheet builders.
- `_sheet_name()` — Excel's constraints (≤31 chars, no `[]:*?/\`, unique).
- `_autosize`, `_style_header`, and the style constants.

Imports its canonical mapping from `extract.py`; does not define its own.

---

## `app/main.py` — API and wiring

- Upload routing: `.pdf` → `parse_pdf`, `.csv/.tsv/.txt` → `parse_delimited`,
  anything else → 400.
- Persistence goes through `store`; `main.py` holds no state of its own.
- `_export_set()` — resolves `?ids=`, defaulting to the caller's last 24 hours.
  Without it, persistence silently turns "export all" into "the entire history".
- `_guard_failsafe()` — raises **409** on any export whose failsafe failed. The
  UI disables the buttons, but the endpoints are directly reachable, so this is
  the real enforcement.
- Endpoints: upload, list, fetch one, per-statement TSV/XLSX, combined
  `export.tsv`/`export.xlsx`, original source file, delete.
- `NoCacheStatic` — serves the frontend `no-store`.

---

## `app/static/`

**`app.js`** — no framework, no build. `state` holds the loaded statements;
`renderList` / `renderViewer` / `renderTable` / `renderFailsafe` / `renderCleanup`
redraw from it. `setExportEnabled()` disables Copy/Excel/TSV while the failsafe
fails. `copy()` falls back to a hidden textarea outside secure contexts. `money()`
is pinned to `en-US` so separators match the source documents.

**`style.css`** — CSS custom properties for both themes, then components
(topbar, sidebar, drop zone, table, failsafe banner, cleanup tags, toast).
Uses `color-mix()` for status tints so one token drives every state colour.

**`index.html`** — skeleton only; every dynamic region is an empty element that
`app.js` fills.

---

## `scripts/`

**`carriers_report.py`** — parses every file in `statements/`, buckets each
carrier by measured outcome (`verified` / `unverified` / `no-table` / `needs-ocr`
/ `crash`), and regenerates `CARRIERS.md`. Handles PDFs and CSVs. Run after any
parsing change.

**`failsafe_report.py`** — per-file table of failsafe status, exported total,
filename total, statement total and commission column; flags where the two
references disagree. Ends with a list of files whose export is blocked.

Together these are the regression suite — there is no `pytest`.

---

## Where to make common changes

| Task | File |
|---|---|
| Add a carrier template | `extract.py` → `TEMPLATES` |
| Change OCR accuracy settings | `ocr.py` → constants; re-run `scripts/ocr_report.py` |
| Fix a mis-detected column | `extract.py` → `_CANONICAL_PATTERNS` |
| Change date/amount cleaning | `tabular.py` → `_plan_column`, `_clean_value` |
| Drop another junk row | `tabular.py` → `DROP_ROW_MARKERS` |
| Change Excel typing/formats | `excel.py` → `_write` |
| Add an endpoint | `main.py` (remember it is behind the gate) |
| Change what anonymous callers may fetch | `auth.py` → `PUBLIC_PATHS` |
| Add a stored field | `store.py` → `SCHEMA` + `save_statement` |
| Change the UI | `static/app.js` + `static/style.css` |
| Add a new source type | produce a `Statement`; call `_finalise()` |
