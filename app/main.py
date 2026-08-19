"""FastAPI app: upload commission statements, extract tables, serve the UI."""

from __future__ import annotations

import shutil
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .excel import CANONICAL_FIELDS, build_workbook, canonical_map
from .extract import parse_pdf
from .tabular import parse_delimited

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# PDFs go through the layout engine; delimited exports through the cleaner.
PDF_SUFFIX = ".pdf"
DELIMITED_SUFFIXES = (".csv", ".tsv", ".txt")


def _guard_failsafe(statements: list[dict]) -> None:
    """Refuse to hand out an export whose amounts do not reconcile.

    The UI disables the buttons, but the endpoints are reachable directly, and
    the whole point of the failsafe is that unreconciled figures never leave the
    app. 409 rather than 400: the request is valid, the state is not.
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
UPLOAD_DIR = Path(tempfile.gettempdir()) / "commission_statements"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Commission Statement Reader")

# In-memory store. Swap for SQLite when you want statements to survive a restart.
STATEMENTS: dict[str, dict] = {}


@app.post("/api/statements")
async def upload(file: UploadFile = File(...)):
    name = (file.filename or "").lower()
    if name.endswith(PDF_SUFFIX):
        suffix, reader = PDF_SUFFIX, parse_pdf
    elif name.endswith(DELIMITED_SUFFIXES):
        suffix, reader = Path(name).suffix, parse_delimited
    else:
        raise HTTPException(400, "Upload a PDF, CSV or TSV.")

    sid = uuid.uuid4().hex[:12]
    dest = UPLOAD_DIR / f"{sid}{suffix}"
    with dest.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)

    try:
        stmt = reader(str(dest), file.filename)
    except Exception as exc:  # surface parse failures to the UI, don't 500
        dest.unlink(missing_ok=True)
        raise HTTPException(422, f"Could not read that file: {exc}") from exc

    payload = stmt.to_dict()
    payload["id"] = sid
    payload["tsv"] = stmt.to_tsv()
    STATEMENTS[sid] = payload
    return payload


@app.get("/api/statements")
async def list_statements():
    return [
        {
            "id": s["id"],
            "filename": s["filename"],
            "row_count": len(s["rows"]),
            "ok": all(c["ok"] for c in s["checks"]) and not s["warnings"],
            "failsafe": (s.get("payout") or {}).get("status", "no_reference"),
        }
        for s in STATEMENTS.values()
    ]


@app.get("/api/statements/{sid}")
async def get_statement(sid: str):
    if sid not in STATEMENTS:
        raise HTTPException(404, "Unknown statement.")
    return STATEMENTS[sid]


@app.get("/api/statements/{sid}/tsv")
async def get_tsv(sid: str):
    if sid not in STATEMENTS:
        raise HTTPException(404, "Unknown statement.")
    _guard_failsafe([STATEMENTS[sid]])
    stem = Path(STATEMENTS[sid]["filename"]).stem
    return PlainTextResponse(
        STATEMENTS[sid]["tsv"],
        headers={"Content-Disposition": f'attachment; filename="{stem}.tsv"'},
    )


@app.get("/api/statements/{sid}/xlsx")
async def get_xlsx(sid: str):
    if sid not in STATEMENTS:
        raise HTTPException(404, "Unknown statement.")
    _guard_failsafe([STATEMENTS[sid]])
    stem = Path(STATEMENTS[sid]["filename"]).stem
    return StreamingResponse(
        build_workbook([STATEMENTS[sid]]),
        media_type=XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{stem}.xlsx"'},
    )


@app.get("/api/export.xlsx")
async def export_all_xlsx():
    """Every statement: one sheet each, plus combined rows and validation."""
    if not STATEMENTS:
        raise HTTPException(404, "Nothing uploaded yet.")
    _guard_failsafe(list(STATEMENTS.values()))
    return StreamingResponse(
        build_workbook(list(STATEMENTS.values())),
        media_type=XLSX_MIME,
        headers={"Content-Disposition": 'attachment; filename="statements.xlsx"'},
    )


@app.get("/api/statements/{sid}/pdf")
async def get_pdf(sid: str):
    """Serve the original upload. Only PDFs render in the side-by-side pane."""
    for suffix in (PDF_SUFFIX, *DELIMITED_SUFFIXES):
        path = UPLOAD_DIR / f"{sid}{suffix}"
        if path.exists():
            media = "application/pdf" if suffix == PDF_SUFFIX else "text/plain"
            return FileResponse(path, media_type=media)
    raise HTTPException(404, "Source file not found.")


@app.get("/api/export.tsv")
async def export_all():
    """Every statement on the shared cross-carrier schema.

    Carriers do not share column names, so rows are mapped onto CANONICAL_FIELDS
    rather than concatenated under one carrier's headers.
    """
    if not STATEMENTS:
        raise HTTPException(404, "Nothing uploaded yet.")
    out = ["\t".join(["Source", "Carrier", *CANONICAL_FIELDS])]
    for s in STATEMENTS.values():
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


@app.delete("/api/statements/{sid}")
async def delete_statement(sid: str):
    STATEMENTS.pop(sid, None)
    for suffix in (PDF_SUFFIX, *DELIMITED_SUFFIXES):
        (UPLOAD_DIR / f"{sid}{suffix}").unlink(missing_ok=True)
    return {"ok": True}


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
