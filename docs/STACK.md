# Stack

Deliberately small. Five runtime dependencies, no build step, no database, no
container. The reasoning for each choice is recorded because several were
contested and the alternatives are still reasonable.

---

## Runtime

| Layer | Choice | Version |
|---|---|---|
| Language | Python | 3.14 |
| Web framework | FastAPI | 0.115.6 |
| ASGI server | uvicorn (`[standard]`) | 0.34.0 |
| Multipart uploads | python-multipart | 0.0.20 |
| PDF text + geometry | pdfplumber | 0.11.4 |
| Excel writer | openpyxl | 3.1.5 |
| OCR (scans) | pytesseract + Tesseract 5.4 | 0.3.13 |
| Frontend | Plain HTML / CSS / JS | — |
| Storage | In-process dict | — |

```bash
pip install -r requirements.txt
winget install UB-Mannheim.TesseractOCR      # OCR engine; pip cannot install it
python -m uvicorn app.main:app --reload --port 8000
```

---

## Why these

### Python, not Node/Go
The PDF and OCR ecosystem is Python. `pdfplumber`, `pypdf`, `pdfminer`,
`pytesseract`, and the AWS Textract SDK all live here, and the OCR tier this app
will eventually need has no comparable equivalent elsewhere.

### pdfplumber
Chosen because it exposes **per-word coordinates** (`x0, x1, top, bottom`), not
just text. The entire column engine is interval arithmetic over those boxes —
`pdftotext -layout` was tried first and produced unusable output on rotated
pages (`1FMc0igc05aFlNlin.aITnDchiir8ad3l6IS3t8nsurance`). Without coordinates
this app is not buildable.

`pypdf` was used briefly to normalise rotation via
`transfer_rotation_to_content()`; that made word grouping *worse* (glyphs became
individually placed) and was dropped. Rotation is now handled by negating the
span axis instead, and `pypdf` is no longer a dependency.

### FastAPI + uvicorn
Async, typed, trivial multipart handling, and it can serve the static frontend
from the same process — one command, one port, no CORS. `--reload` gives edit-and-refresh.

### Plain HTML/CSS/JS, not React/Next.js
This was a real decision and it went against the original plan.

A no-build frontend runs immediately with no `npm install`, no bundler, no
`node_modules`, and no toolchain to break — which matters for something meant to
be run locally by whoever needs it. Design quality is unaffected; the UI has a
themed design-token system, responsive layout, and dark/light support in ~475
lines of CSS.

Next.js remains a clean later swap: the API is a plain REST surface and the
frontend is one page with no server-side coupling. Adopt it when routing,
componentisation or shared state justify a build step.

Consequence to know about: browser caching. Static assets are served
`Cache-Control: no-store` (`NoCacheStatic` in `app/main.py`) because a cached
`index.html` against a freshly-edited `app.js` silently half-breaks the UI —
this cost real debugging time before being fixed.

### pytesseract + Tesseract
16 carriers ship scans with no text layer. Tesseract is weak at *table* structure,
which would normally rule it out — but that does not matter here, because
`image_to_data` returns per-word bounding boxes and the existing column engine
already turns word boxes into tables. So the only job OCR has is producing word
boxes, which Tesseract does well and for free, locally, with no client data
leaving the machine.

The engine binary is not pip-installable, so it is the one manual setup step.
Accuracy settings (300 DPI, `--psm 6`, 25% confidence floor) were chosen by
sweeping `scripts/ocr_report.py`, scored against the amounts in the filenames.

### openpyxl
The Excel requirement is *typed cells* — real numbers with `#,##0.00`, percents
as fractions, real dates — so figures are summable rather than text that merely
looks right. `openpyxl` also does frozen panes, autofilter, and per-cell fonts
and borders. `xlsxwriter` writes only; `openpyxl` reads too, which the
verification scripts use to assert what was actually written.

### In-memory storage, not SQLite
Deliberate for now: no schema, no migrations, no file locking. `STATEMENTS` is a
single dict in `app/main.py` and swapping it for SQLite is a contained change
behind the same REST surface. The cost is real — a restart clears the list.

---

## Not used, and why

| Not used | Why |
|---|---|
| **AWS Textract** | Tesseract now covers the scans (20 of 21 readable). Textract is stronger on tables but costs ~$1.50/1k pages and sends client PII to AWS. Worth revisiting only for the 4 carriers whose OCR figures fail the failsafe. |
| **LLM extraction** | Considered as the structuring layer. The self-validating auto-detector reached 26 carriers without it, so it stays a fallback for layouts geometry cannot crack rather than the primary path. |
| **A trained model** (LayoutLMv3 / Donut) | Evaluated and rejected at current volume. Break-even against a warm T4 (~$255/mo) is roughly 18,000 statements/month, and the labelled data would have to come from somewhere — realistically from running the API path first. Revisit at high volume, distilling from validated extractions. |
| **pandas** | Never needed. Rows are `list[dict[str, str]]`; cleaning is per-column plans, and typing happens at the Excel boundary. Adding pandas would import a dataframe model the app does not use. |
| **Docker** | Runs locally with two commands on the developer's own Python. |
| **A test framework** | See below. |

---

## Verification instead of unit tests

There is no `pytest` suite. The regression check is two scripts that sweep every
real statement in `statements/` and print measured results:

```bash
python scripts/carriers_report.py    # per-carrier status → regenerates CARRIERS.md
python scripts/failsafe_report.py    # per-file payout reconciliation
```

This is a deliberate fit to the problem: correctness here means "does this
extract the right numbers from 57 real carrier documents", which is an
integration property over real fixtures, not a unit property. The statements
themselves are the fixtures, and the failsafe is the assertion.

The honest limitation: those fixtures are gitignored client data, so the checks
are not reproducible outside this machine and cannot run in CI. Redacted
synthetic fixtures would fix that and are the obvious next step if this needs
to become a real test suite.

---

## Constraints worth knowing

- **Windows-first.** Developed on Windows 11; paths and the `.claude/launch.json`
  runner assume it. Nothing is OS-specific in the code itself.
- **`statements/` is gitignored**, along with `*.csv`, `*.tsv`, `*.xls[x]` and
  Excel lock files (`~$*`) — it holds real customer names, policy numbers and
  amounts.
- **No authentication.** Do not expose the app publicly as-is; anyone reaching it
  can read and download every loaded statement.
