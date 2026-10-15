"""FastAPI app: upload commission statements, extract tables, serve the UI.

Statements are persisted in SQLite (app/store.py) rather than held in memory, so
they survive a restart and form a searchable history. Every request passes through
the auth gate in app/auth.py first.
"""

from __future__ import annotations

import hashlib
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from . import auth, extract, image_ocr, store
from .excel import CANONICAL_FIELDS, build_workbook, canonical_map
from .extract import parse_pdf
from .tabular import parse_delimited

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# PDFs go through the layout engine; delimited exports through the cleaner.
PDF_SUFFIX = ".pdf"
DELIMITED_SUFFIXES = (".csv", ".tsv", ".txt")

# An upload is read into memory by the parser and rasterised page-by-page for OCR,
# so an unbounded file is a trivial way to exhaust the box.
MAX_UPLOAD_BYTES = 40 * 1024 * 1024

# Prefix for the warning an edit raises. Used to find and replace a previous
# edit warning rather than stacking one per save.
EDIT_WARNING = "EDITED:"


def _guard_failsafe(statements: list[dict]) -> None:
    """Refuse to hand out an export whose amounts do not reconcile.

    The UI disables the buttons, but the endpoints are reachable directly, and the
    whole point of the failsafe is that unreconciled figures never leave the app.
    409 rather than 400: the request is valid, the state is not.

    This runs on history exports too. A statement that failed reconciliation on
    Tuesday must not become exportable on Friday by arriving through a different
    endpoint.
    """
    bad = [s for s in statements if (s.get("payout") or {}).get("status") == "mismatch"]
    if bad:
        names = ", ".join(s["filename"] for s in bad)
        raise HTTPException(
            409,
            f"Failsafe failed for {names}. The commission amounts do not match "
            f"the statement's declared total, so this export is blocked.",
        )


BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Commission Statement Reader")
app.middleware("http")(auth.gate)


@app.on_event("startup")
def _startup() -> None:
    store.init()
    store.purge_expired_sessions()


def _me(request: Request) -> dict:
    user = auth.current_user(request)
    if user is None:                    # the gate should have caught this already
        raise HTTPException(401, "Not signed in.")
    return user


def _load(sid: str) -> dict:
    stmt = store.get_statement(sid)
    if stmt is None:
        raise HTTPException(404, "Unknown statement.")
    return stmt


# --- auth --------------------------------------------------------------------

@app.get("/login")
async def login_page(request: Request):
    if auth.current_user(request):
        return JSONResponse({"ok": True}, status_code=303, headers={"Location": "/"})
    return FileResponse(
        STATIC_DIR / "login.html", headers={"Cache-Control": "no-store"}
    )


@app.post("/api/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    wait = store.lock_remaining(email)
    if wait:
        raise HTTPException(
            429,
            f"Too many failed attempts. Try again in {max(1, wait // 60)} minute(s).",
        )

    user = store.verify_user(email, password)
    if user is None:
        raise HTTPException(401, "Wrong email or password.")

    token = store.create_session(user["id"], request.headers.get("user-agent", ""))
    response = JSONResponse({"ok": True, "user": user})
    auth.set_session_cookie(response, token, secure=request.url.scheme == "https")
    return response


@app.post("/api/logout")
async def logout(request: Request):
    token = request.cookies.get(auth.COOKIE_NAME, "")
    if token:
        store.delete_session(token)
    response = JSONResponse({"ok": True})
    auth.clear_session_cookie(response)
    return response


@app.get("/api/me")
async def me(request: Request):
    return _me(request)


@app.get("/api/health")
async def health():
    return {"ok": True}


# --- statements --------------------------------------------------------------

@app.post("/api/statements")
async def upload(request: Request, file: UploadFile = File(...)):
    user = _me(request)
    name = (file.filename or "").lower()
    if name.endswith(PDF_SUFFIX):
        suffix, reader = PDF_SUFFIX, parse_pdf
    elif name.endswith(DELIMITED_SUFFIXES):
        suffix, reader = Path(name).suffix, parse_delimited
    else:
        raise HTTPException(400, "Upload a PDF, CSV or TSV.")

    sid = uuid.uuid4().hex[:12]
    dest = store.UPLOAD_DIR / f"{sid}{suffix}"
    dest.parent.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha256()
    written = 0
    with dest.open("wb") as fh:
        while chunk := await file.read(1024 * 1024):
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                fh.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(
                    413,
                    f"That file is larger than "
                    f"{MAX_UPLOAD_BYTES // (1024 * 1024)}MB.",
                )
            digest.update(chunk)
            fh.write(chunk)

    duplicate = store.duplicate_of(digest.hexdigest())

    try:
        stmt = reader(str(dest), file.filename)
    except Exception as exc:  # surface parse failures to the UI, don't 500
        dest.unlink(missing_ok=True)
        raise HTTPException(422, f"Could not read that file: {exc}") from exc

    payload = stmt.to_dict()
    payload["tsv"] = stmt.to_tsv()
    payload = store.save_statement(
        sid, payload, user["id"], suffix, digest.hexdigest()
    )
    payload["uploaded_by"] = user["display_name"]
    if duplicate:
        payload["duplicate_of"] = duplicate
    return payload


@app.get("/api/statements")
async def list_statements(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    search: str = "",
    failsafe: str = "",
    mine: bool = False,
    since: str = "",
):
    """Summaries for the sidebar and the history view.

    Summaries only - the payload is fetched per statement on selection. Loading
    every payload here was fine for the handful once held in memory and is not
    fine against a growing history.
    """
    user = _me(request)
    items, total = store.list_statements(
        limit=min(limit, 200),
        offset=offset,
        search=search,
        failsafe=failsafe,
        uploader=user["id"] if mine else None,
        since=since,
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/api/carriers")
async def carriers(request: Request):
    _me(request)
    return store.carriers()


@app.get("/api/statements/{sid}")
async def get_statement(request: Request, sid: str):
    _me(request)
    return _load(sid)


@app.get("/api/statements/{sid}/tsv")
async def get_tsv(request: Request, sid: str):
    _me(request)
    stmt = _load(sid)
    _guard_failsafe([stmt])
    stem = Path(stmt["filename"]).stem
    return PlainTextResponse(
        stmt.get("tsv", ""),
        headers={"Content-Disposition": f'attachment; filename="{stem}.tsv"'},
    )


@app.get("/api/statements/{sid}/xlsx")
async def get_xlsx(request: Request, sid: str):
    _me(request)
    stmt = _load(sid)
    _guard_failsafe([stmt])
    stem = Path(stmt["filename"]).stem
    return StreamingResponse(
        build_workbook([stmt]),
        media_type=XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{stem}.xlsx"'},
    )


@app.get("/api/statements/{sid}/pdf")
async def get_pdf(request: Request, sid: str):
    """Serve the original upload. Only PDFs render in the side-by-side pane."""
    _me(request)
    path = store.source_path(sid)
    if path is None:
        raise HTTPException(404, "Source file not found.")
    media = "application/pdf" if path.suffix.lower() == PDF_SUFFIX else "text/plain"
    return FileResponse(path, media_type=media)


EDIT_TOL = 0.01  # cents; these are printed figures, not computed ones


def _apply_edit(stmt: dict, columns: list[str], rows: list[dict]) -> dict:
    """Rebuild a payload around user-edited columns and rows.

    The failsafe verdict in `payout` is carried across untouched. It records
    whether the *extraction* matched the statement the carrier sent, which an
    edit cannot change after the fact - it must be able neither to clear a real
    mismatch nor to invent one.

    What an edit can do is move the table away from the figure the failsafe
    measured, so that difference is reported as a warning instead. Warnings from
    the original parse are kept; only a previous edit warning is replaced, so
    editing twice does not stack duplicates.
    """
    payout = stmt.get("payout") or {}
    extracted = payout.get("exported_total")

    column, current = extract.commission_total(
        stmt.get("template", ""), columns, rows
    )

    warnings = [w for w in stmt.get("warnings", []) if not w.startswith(EDIT_WARNING)]
    if extracted is not None and current is None:
        warnings.insert(
            0,
            f"{EDIT_WARNING} the {payout.get('commission_column') or 'commission'}"
            f" column no longer holds any figures, so this export no longer"
            f" carries the {extracted:,.2f} that was checked against the"
            f" statement.",
        )
    elif (
        extracted is not None
        and current is not None
        and abs(current - extracted) > EDIT_TOL
    ):
        warnings.insert(
            0,
            f"{EDIT_WARNING} {column} now adds up to {current:,.2f}, but"
            f" {extracted:,.2f} was what the statement was checked against."
            f" The difference is {current - extracted:+,.2f}.",
        )

    edited = {
        **stmt,
        "columns": columns,
        "rows": rows,
        "warnings": warnings,
        "edited_total": current,
    }
    edited["tsv"] = _rebuild_tsv(edited)
    return edited


def _rebuild_tsv(stmt: dict) -> str:
    """Regenerate the TSV after an edit.

    `tsv` is computed once at upload and stored. Without this the clipboard and
    the TSV export would keep serving the pre-edit table while the screen showed
    the edited one - the kind of disagreement nobody notices until it is pasted
    into a spreadsheet.
    """
    columns = stmt.get("columns", [])
    lines = ["\t".join(columns)]
    lines += ["\t".join(r.get(c, "") for c in columns) for r in stmt.get("rows", [])]
    totals = stmt.get("totals") or {}
    if totals:
        lines.append("\t".join(totals.get(c, "") for c in columns))
    return "\n".join(lines) + "\n"


@app.patch("/api/statements/{sid}")
async def edit_statement(request: Request, sid: str):
    """Save a user-edited table. The failsafe verdict is preserved, not re-run."""
    user = _me(request)
    stmt = _load(sid)

    body = await request.json()
    columns = body.get("columns")
    rows = body.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise HTTPException(400, "Expected 'columns' and 'rows'.")
    if not all(isinstance(c, str) for c in columns):
        raise HTTPException(400, "Column names must be strings.")
    if not all(isinstance(r, dict) for r in rows):
        raise HTTPException(400, "Rows must be objects.")

    edited = _apply_edit(stmt, columns, rows)
    saved = store.update_statement(sid, edited, user["display_name"])
    saved["edited"] = True
    return saved


@app.post("/api/statements/{sid}/reparse")
async def reparse_statement(request: Request, sid: str):
    """Re-extract from the stored source file, restricted to a drawn region.

    A re-parse replaces the table wholesale, so it also clears any edits - the
    rows they applied to no longer exist. `original` is reset with it, because
    what it means is "the table as extracted", and this *is* a fresh extraction.
    Send an empty body or `{"region": null}` to go back to reading the whole page.
    """
    _me(request)
    stmt = _load(sid)

    path = store.source_path(sid)
    if path is None:
        raise HTTPException(404, "The source file for this statement is gone.")
    if path.suffix.lower() != PDF_SUFFIX:
        raise HTTPException(400, "Region selection only applies to PDFs.")

    body = await request.json() if await request.body() else {}
    region = body.get("region")
    if region is not None:
        try:
            region = {
                "page": int(region["page"]),
                **{k: float(region[k]) for k in ("x0", "y0", "x1", "y1")},
            }
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "A region needs page, x0, y0, x1 and y1.")
        if region["page"] < 0 or not all(
            0.0 <= region[k] <= 1.0 for k in ("x0", "y0", "x1", "y1")
        ):
            raise HTTPException(400, "Region coordinates must be fractions 0-1.")
        if region["x1"] - region["x0"] < 0.01 or region["y1"] - region["y0"] < 0.01:
            raise HTTPException(400, "That region is too small to read.")

    try:
        parsed = parse_pdf(str(path), stmt["filename"], region=region)
    except Exception as exc:
        raise HTTPException(422, f"Could not re-read that file: {exc}") from exc

    payload = parsed.to_dict()
    payload["tsv"] = parsed.to_tsv()
    payload["id"] = sid

    # A re-parse is a new extraction, so the edit markers that described the old
    # table are cleared rather than carried onto rows they no longer match.
    # replace_statement keeps the uploader and the original upload time: this is
    # the same file read again, not a new submission.
    store.replace_statement(sid, payload)
    return _load(sid)


@app.post("/api/statements/{sid}/revert")
async def revert_statement(request: Request, sid: str):
    """Restore the table exactly as it was first extracted."""
    _me(request)
    _load(sid)
    payload = store.revert_statement(sid)
    if payload is None:
        raise HTTPException(409, "This statement has not been edited.")
    return _load(sid)


@app.delete("/api/statements/{sid}")
async def delete_statement(request: Request, sid: str):
    _me(request)
    return {"ok": store.delete_statement(sid)}


# --- exports -----------------------------------------------------------------

def _export_set(user: dict, ids: str) -> list[dict]:
    """Resolve the ?ids= parameter to statements, defaulting to recent uploads.

    Persistence changed what "export everything" can mean. In memory it meant
    today's uploads; against history it would mean every statement ever uploaded
    by anyone, which is not what the button promises. So the UI sends the ids of
    its working set, and an absent ids= falls back to this caller's last 24 hours.
    """
    wanted = [i for i in (ids or "").split(",") if i.strip()]
    if not wanted:
        wanted = store.recent_ids(user["id"])
    statements = [s for s in (store.get_statement(i) for i in wanted) if s]
    if not statements:
        raise HTTPException(404, "Nothing to export.")
    return statements


# --- simple OCR --------------------------------------------------------------
# Free text from a single image. Nothing is stored: the image is transcribed and
# dropped. Sync `def` routes on purpose, so the slow OCR/API call runs in the
# threadpool instead of blocking the event loop for every other user.

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff")
MAX_IMAGE_BYTES = 20 * 1024 * 1024


def _read_image(file: UploadFile) -> bytes:
    if not (file.filename or "").lower().endswith(IMAGE_SUFFIXES):
        raise HTTPException(400, "Upload a PNG, JPG, WEBP, GIF, BMP or TIFF image.")
    data = file.file.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, f"That image is larger than "
                                 f"{MAX_IMAGE_BYTES // (1024 * 1024)}MB.")
    return data


@app.get("/api/ocr/options")
def ocr_options():
    return {"engines": image_ocr.engines(), "languages": image_ocr.languages()}


@app.post("/api/ocr")
def ocr_image(
    file: UploadFile = File(...),
    engine: str = Form("claude"),
    language: str = Form("auto"),
):
    data = _read_image(file)
    try:
        return image_ocr.transcribe(data, engine, language)
    except image_ocr.OCRError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/ocr/deposit-slip")
def ocr_deposit_slip(file: UploadFile = File(...)):
    """Read a deposit slip into fields. The arithmetic check is done by the UI,
    against the figures as read - see the note above SLIP_SYSTEM_PROMPT."""
    data = _read_image(file)
    try:
        return image_ocr.deposit_slip(data)
    except image_ocr.OCRError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/export.xlsx")
async def export_all_xlsx(request: Request, ids: str = ""):
    """One sheet per statement, plus combined rows and validation."""
    statements = _export_set(_me(request), ids)
    _guard_failsafe(statements)
    return StreamingResponse(
        build_workbook(statements),
        media_type=XLSX_MIME,
        headers={"Content-Disposition": 'attachment; filename="statements.xlsx"'},
    )


@app.get("/api/export.tsv")
async def export_all(request: Request, ids: str = ""):
    """Every selected statement on the shared cross-carrier schema.

    Carriers do not share column names, so rows are mapped onto CANONICAL_FIELDS
    rather than concatenated under one carrier's headers.
    """
    statements = _export_set(_me(request), ids)
    _guard_failsafe(statements)
    out = ["\t".join(["Source", "Carrier", *CANONICAL_FIELDS])]
    for s in statements:
        mapping = canonical_map(s.get("template", ""), s.get("columns", []))
        if not mapping:
            continue
        for row in s["rows"]:
            out.append(
                "\t".join(
                    [
                        s["filename"],
                        s.get("template", ""),
                        *(row.get(mapping.get(f, ""), "") for f in CANONICAL_FIELDS),
                    ]
                )
            )
    return PlainTextResponse(
        "\n".join(out) + "\n",
        headers={"Content-Disposition": 'attachment; filename="all_statements.tsv"'},
    )


class NoCacheStatic(StaticFiles):
    """Serve the UI without browser caching.

    With --reload the server picks up edits, but the browser keeps serving a
    cached index.html/app.js, so changes appear to have no effect until a hard
    reload. Not worth a cache-busting build step for a no-build frontend.
    """

    def is_not_modified(self, *args, **kwargs) -> bool:
        return False

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store"
        return response


app.mount("/", NoCacheStatic(directory=STATIC_DIR, html=True), name="static")
