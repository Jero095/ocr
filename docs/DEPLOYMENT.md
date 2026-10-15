# Deployment

How the system is put together, how it is built into a container, and what
Railway needs. Written to be followed in order by someone who has not touched
the code for a while.

For per-file responsibilities in more depth, see [FILES.md](FILES.md); this
document covers the same ground from the deployment angle and adds a complete
function reference.

---

## 1. Architecture

### The shape of it

Two ingestion paths converge on one `Statement` object. Everything downstream —
validation, storage, the API, Excel, the UI — consumes that one shape, so adding
a third source type means producing a `Statement` and nothing else.

```
             upload (PDF / CSV / TSV)
                        │
        ┌───────────────┴───────────────┐
        │                               │
  app/extract.py                  app/tabular.py
  geometry engine                 delimiter sniff,
  templates, auto-detect          per-column cleaning
        │                               │
        └──> app/ocr.py                 │
             (only when a page has      │
              no text layer)            │
        │                               │
        └───────────────┬───────────────┘
                        ▼
                   Statement
                        │
              _finalise() → PayoutCheck      ← the failsafe
                        │
                app/store.py                 ← SQLite + the volume
                        │
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
   app/main.py     app/excel.py    app/static/
   REST API        .xlsx builder   the UI
   (behind app/auth.py)
```

### A request, end to end

`POST /api/statements` with a PDF:

1. **`auth.gate`** middleware resolves the session cookie. No session → 401 JSON
   for `/api/*`, 303 to `/login` for navigation.
2. **`main.upload`** routes by extension, streams the file to
   `data/uploads/<id><ext>` while hashing it, and rejects anything over 40 MB.
3. **`extract.parse_pdf`** opens the PDF. For each page with no text layer it
   calls **`ocr.page_words`**, which rasterises at 300 DPI, picks a rotation and
   returns word boxes in PDF points — the same shape pdfplumber returns, so
   everything after this is identical for scans and digital files.
4. Geometry: words → lines → bands → columns → cells.
5. Recognition: a hand-written template if its header tokens match, otherwise
   `_auto_detect` tries every plausible header band and keeps whichever table
   best reconciles against the statement's own totals.
6. **`_finalise`** runs the failsafe: sum the commission column that will
   actually reach Excel, compare against references from *outside* the table.
7. **`store.save_statement`** writes the payload as JSON plus indexed columns.
8. The payload returns to the browser and renders.

### Module reference

Line counts are indicative and drift with edits.

#### `app/extract.py` — the core

Owns the `Statement` model, so every other module depends on it.

**Types**

| Name | Purpose |
|---|---|
| `Statement` | The one shape everything consumes: columns, rows, totals, subtotals, checks, payout, warnings |
| `Check` | One per-column comparison of the row sum against the table's own totals row |
| `PayoutCheck` | The failsafe verdict: `match` / `mismatch` / `no_reference` / `no_amounts` |
| `Template` | Per-carrier config only — header tokens, row pattern, canonical mapping. Geometry is never per-carrier |

**Geometry**

| Function | What it does |
|---|---|
| `_row_axis` | Position along the axis that separates lines |
| `_span` | Extent along the axis that separates columns. Negated on rotated pages so ascending always means visual left-to-right |
| `_text` | Reverses character order on rotated pages |
| `_lines` | Groups words into lines within `LINE_TOL` |
| `_bands` | Groups lines into bands within `BAND_GAP`, then calls the pass below |
| `_merge_header_bands` | Joins consecutive *header-like* bands within `HEADER_MERGE_RATIO` × character height. Content-keyed, because some carriers lead their header lines wider apart than their data rows |
| `_char_height` | Median glyph height — the font-size proxy that ratio is measured against |
| `_merge_spans` | Merges overlapping intervals into column extents |
| `_columns` | Derives column names and extents from a header band |
| `_cells` | Assigns each word to the column it overlaps **most**. Never a distance merge |
| `_crop` | Drops words outside a region given as fractions of the page. Keeps words that *overlap* the box, not only those contained by it |

**Recognition and cleaning**

| Function | What it does |
|---|---|
| `parse_pdf` | Entry point. Per-page rotation, OCR fallback, template or auto-detect, finalise |
| `_auto_detect` | Scores every plausible header band; the arithmetic decides the layout |
| `_score` | Passing checks, row count, for a candidate table |
| `_looks_like_header` / `_looks_like_data` | The gates. Headers are labels, not figures |
| `split_trailing_total` | Catches an unlabelled totals row by arithmetic — it is a total only if every figure equals the column sum above it |
| `pick_totals_row` | The last totals-like row *carrying figures*; a numberless "TOTAL …" line is a caption |
| `_clean`, `to_float`, `is_percent` | Text repair and numeric parsing; parentheses mean negative |
| `carrier_from_filename` | Strips the amount and `(1)` suffixes |
| `_validate` | Per-column and per-row rate checks |

**Canonical mapping and the failsafe**

| Function | What it does |
|---|---|
| `infer_canonical` | Maps an auto-detected carrier's columns onto the shared schema by keyword, skipping YTD and prior-period columns |
| `canonical_map` | Template mapping if there is one, else inference |
| `filename_amount` | The amount in the filename — a reference from outside the table |
| `declared_totals` | Labelled totals in the statement text, ranked so prior balances cannot produce a false pass |
| `commission_total` | The commission column and what it currently sums to. Shared by the failsafe and the edit path so they cannot drift apart |
| `_payout_check` | The reconciliation itself |
| `_finalise` | Called on **every** exit path, so the failsafe can never be skipped |

#### `app/ocr.py` — scans

| Function | What it does |
|---|---|
| `available` | `(usable, reason)`. The reason reaches the UI, so a missing engine explains itself instead of looking like a parse failure |
| `_find_tesseract` | PATH, then `TESSERACT_CMD`, then known Windows paths. **PATH-first is why the container needs no code change** |
| `_configured` | Imports pytesseract pointed at the binary found |
| `_words` | One `image_to_data` result → word dicts in points, with mean confidence |
| `page_words` | Rasterise, choose rotation by measured confidence, OCR, scale to points |

#### `app/tabular.py` — CSV/TSV

| Function | What it does |
|---|---|
| `sniff` | Delimiter by column-count consistency |
| `to_mdy` | Normalises several date shapes to `mm/dd/yyyy`, discarding time |
| `_plan_column` | Classifies text / date / money / percent and decides rescaling |
| `_resolve_percent_scale` | Derives an ambiguous percent scale from amount ÷ base rather than assuming |
| `_drop_marker` | Removes non-transaction rows **by content, never by position** |
| `_split_totals_row` | Keeps a trailing totals row out of the data |
| `_clean_value`, `format_amount` | Apply the plan |
| `parse_delimited` | Entry point; produces the same `Statement` |

#### `app/store.py` — persistence

Plain `sqlite3`, no ORM: `Statement.to_dict()` is already JSON, so a statement
is one blob plus indexed columns for listing.

| Group | Functions |
|---|---|
| Paths and setup | `DATA_DIR` (env-overridable), `connect` (WAL + foreign keys), `init` |
| Passwords | `hash_password`, `_password_ok`, `verify_user`, `set_password` |
| Users | `create_user`, `get_user`, `list_users`, `user_count` |
| Lockout | `lock_remaining`, `_record_failure` |
| Sessions | `create_session`, `session_user`, `delete_session`, `purge_expired_sessions` |
| Statements | `save_statement`, `get_statement`, `list_statements`, `delete_statement`, `source_path`, `duplicate_of`, `recent_ids`, `carriers` |
| Editing | `update_statement` (saves an edit, preserves `original` and the verdict), `revert_statement`, `replace_statement` (re-parse: new table, same uploader) |
| Migration | `_migrate` — adds columns to databases created before a release; `CREATE TABLE IF NOT EXISTS` cannot |

`recent_ids` is small but load-bearing: it is the default export scope, and it
is what stops "export all" from meaning the entire history.

#### `app/auth.py` — the gate

| Function | What it does |
|---|---|
| `gate` | The **single middleware** in front of everything. Not per-route, because the frontend is mounted as a catch-all at `/` and a route guard would leave the UI public |
| `current_user` | The signed-in user for this request |
| `set_session_cookie` | `HttpOnly`, `SameSite=Lax`, and `Secure` derived from the request scheme |
| `clear_session_cookie`, `_wants_json` | Logout; JSON-vs-redirect decision |

`PUBLIC_PATHS` is the complete anonymous surface: `/login`, `/api/login`,
`/api/logout`, `/style.css`, `/favicon.ico`, `/api/health`.

#### `app/excel.py` — workbook

| Function | What it does |
|---|---|
| `build_workbook` | `All Rows` + one sheet per statement + `Validation` |
| `_write` | The typing boundary: real dates, percents as fractions, money as numbers |
| `_statement_sheet`, `_all_rows_sheet`, `_checks_sheet` | Per-sheet builders |
| `_sheet_name`, `_style_header`, `_autosize` | Excel's constraints and formatting |

#### `app/main.py` — API

| Endpoint | Function |
|---|---|
| `GET /login`, `POST /api/login`, `POST /api/logout`, `GET /api/me` | `login_page`, `login`, `logout`, `me` |
| `GET /api/health` | `health` — public, and the container healthcheck |
| `POST /api/statements` | `upload` |
| `GET /api/statements` | `list_statements` — summaries only, paged |
| `GET /api/statements/{id}` `/tsv` `/xlsx` `/pdf`, `DELETE` | `get_statement`, `get_tsv`, `get_xlsx`, `get_pdf`, `delete_statement` |
| `GET /api/export.xlsx`, `/api/export.tsv` | `export_all_xlsx`, `export_all` |
| `PATCH /api/statements/{id}` | `edit_statement` — save an edited table; `_apply_edit` carries the verdict across and reports drift, `_rebuild_tsv` keeps the stored TSV in step |
| `POST /api/statements/{id}/reparse` | `reparse_statement` — re-extract, optionally cropped to a region |
| `POST /api/statements/{id}/revert` | `revert_statement` — restore the table as extracted |
| `GET /api/carriers` | `carriers` |

`_export_set` resolves `?ids=`; `_guard_failsafe` raises **409** on any export whose figures do not reconcile,
including from history. `NoCacheStatic` serves the frontend `no-store`.

#### `app/static/` — the UI

No framework, no build step. `app.js` holds `state`, redraws through
`renderList` / `renderViewer` / `renderTable` / `renderFailsafe` / `renderCleanup`,
and routes every request through `api()` so an expired session redirects to
`/login`. `login.html` is the sign-in page; `style.css` carries both themes.

---

## 2. Build pipeline (Docker)

### Why a Dockerfile and not auto-detection

Railway can infer a Python build. It would produce an image that starts, serves,
parses digital PDFs — and silently fails on every scanned carrier, because
Tesseract is a system package pip cannot install. The failure is quiet: the app
reports "needs OCR" rather than crashing. An explicit Dockerfile keeps that
dependency visible.

### What the image contains

| Layer | Why |
|---|---|
| `python:3.12-slim` | Code needs 3.10+. Pinned so a rebuild in six months is the same runtime |
| `tesseract-ocr`, `-eng`, `-osd` | The engine, English data, and the orientation model. **`osd` is not optional** — `ocr.page_words` calls `image_to_osd` to straighten sideways scans |
| `libgl1`, `libglib2.0-0` | Shared libraries Pillow and pypdfium2 need to rasterise |
| `requirements.txt` before source | Editing code does not invalidate the pip layer |
| non-root `appuser` | Nothing here needs root |
| `DATA_DIR=/app/data` + `VOLUME` | The mount point, declared in the image |

### Build and prove it locally

Do this before paying for anything:

```bash
docker build -t statement-reader .
```

```bash
docker run --rm -p 8000:8000 -e PORT=8000 -v "$(pwd)/data:/app/data" statement-reader
```

Then check, in order:

1. `curl -s -o /dev/null -w "%{http_code}" localhost:8000/api/health` → `200`
2. `/login` → `200`, `/` → `303` (the gate is working)
3. Sign in, upload a digital PDF — it parses
4. **Upload a scanned PDF** — if it OCRs, Tesseract made it into the image. This
   is the step people skip and regret
5. Restart the container; the history is still there (the volume works)

### What is deliberately not in the image

`data/` and `statements/` are excluded by `.dockerignore`. Both hold real client
names, policy numbers and amounts; `data/` also holds password hashes. Without
that exclusion they would be baked into the image and pushed to a registry.

---

## 3. Railway

### Plan

The buyer determines the plan, not the workload:

| Situation | Plan | Cost |
|---|---|---|
| The account can be in your own name | **Hobby** | USD $5/month |
| Someone else owns it and invites you | **Pro** | USD $20/month |

Hobby is single-user — it cannot add collaborators. Pro allows unlimited members
at **no per-seat cost**. Both include usage credit equal to the fee ($5 / $20),
and this app's measured footprint fits inside either.

Measured, for sizing: **224 MB** peak memory during a 7-page OCR, **~82 KB** of
storage per statement, **~107 MB/year** at 100 statements a month. 1 GB of RAM
and a 1 GB volume are generous. Confirm current prices at railway.com/pricing.

### Deploy sequence

1. **Create the project** from the GitHub repo. `railway.json` selects the
   Dockerfile builder and sets the healthcheck automatically.
2. **Create a volume and mount it at `/app/data`.** Do this *before* the first
   real use. This is the single highest-consequence step — see below.
3. **Set variables**: `DATA_DIR=/app/data`. `PORT` is injected by Railway.
4. **Deploy**, then watch the healthcheck pass on `/api/health`.
5. **Create the accounts** from the Railway shell, since there is no signup:
   ```bash
   python scripts/adduser.py add you@example.com --name "You" --admin
   ```
6. **Verify OCR in production** by uploading one scanned statement.

### The volume is the thing to get right

A container filesystem is ephemeral. Without a volume at `/app/data`, **every
deploy and every restart destroys the entire history and all user accounts** —
and it does so silently: the app starts cleanly against an empty database.

`DATA_DIR` is an environment variable specifically so this is something you set
deliberately and can check, rather than a path that happens to line up.

### Health and restarts

`/api/health` is public (it is in `PUBLIC_PATHS`), returns `{"ok": true}`, and
touches neither the database nor the session store — so it answers during
startup. `railway.json` sets `ON_FAILURE` with 3 retries.

### Known operational edges

- **A 7-page scan takes ~64 seconds** of CPU in one synchronous request. Check
  the platform's request timeout before relying on it; if it is shorter, large
  scans need to move to a background job.
- **Egress is billed.** Only Excel exports and PDF previews generate traffic, and
  a workbook is ~30 KB.
- **Pick the region deliberately** and write it down. This is client PII.

---

## 4. What changes when it moves

### It becomes publicly reachable

Today the app exists only inside a Tailscale network — see
[TAILSCALE.md](TAILSCALE.md). On Railway it gets a public URL, and **the login
becomes the only barrier** between the open internet and real client names,
policy numbers and payment amounts.

The login is sound for a small known team: scrypt hashes, server-side revocable
sessions, per-email lockout, no signup route, and the gate covers the UI as well
as the API. But it has never faced the open internet, and it has no rate limit
at the edge, no MFA, and no audit log beyond upload attribution.

**Always-on and publicly-exposed are separate decisions.** Tailscale can run
inside the Railway container, giving 24/7 uptime while staying private. Choose
public exposure because you want it, not because the platform defaulted to it.

### Backups are still not solved

A Railway volume is not a backup — it protects against a restart, not against a
mistake, a bad migration or an account problem. `data/` remains the only copy of
the history and of every account. That is unchanged by moving hosts and is the
next thing worth building.

### What does not change

Parsing, OCR, the failsafe, Excel export and the UI are identical. The container
runs the same code; only where it runs and who can reach it changes.
