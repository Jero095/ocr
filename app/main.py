"""FastAPI app: upload commission statements, extract tables, serve the UI."""

from __future__ import annotations

import shutil
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .extract import parse_pdf

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR = Path(tempfile.gettempdir()) / "commission_statements"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Commission Statement Reader")

# In-memory store. Swap for SQLite when you want statements to survive a restart.
STATEMENTS: dict[str, dict] = {}


@app.post("/api/statements")
async def upload(file: UploadFile = File(...)):
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a PDF.")

    sid = uuid.uuid4().hex[:12]
    dest = UPLOAD_DIR / f"{sid}.pdf"
    with dest.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)

    try:
        stmt = parse_pdf(str(dest), file.filename)
    except Exception as exc:  # surface parse failures to the UI, don't 500
        dest.unlink(missing_ok=True)
        raise HTTPException(422, f"Could not read that PDF: {exc}") from exc

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
    stem = Path(STATEMENTS[sid]["filename"]).stem
    return PlainTextResponse(
        STATEMENTS[sid]["tsv"],
        headers={"Content-Disposition": f'attachment; filename="{stem}.tsv"'},
    )


@app.get("/api/statements/{sid}/pdf")
async def get_pdf(sid: str):
    path = UPLOAD_DIR / f"{sid}.pdf"
    if not path.exists():
        raise HTTPException(404, "PDF not found.")
    return FileResponse(path, media_type="application/pdf")


@app.get("/api/export.tsv")
async def export_all():
    """Every statement combined, with a source column prepended."""
    if not STATEMENTS:
        raise HTTPException(404, "Nothing uploaded yet.")
    out: list[str] = []
    for s in STATEMENTS.values():
        if not out:
            out.append("\t".join(["Source", *s["columns"]]))
        for row in s["rows"]:
            out.append(
                "\t".join([s["filename"], *(row.get(c, "") for c in s["columns"])])
            )
    return PlainTextResponse(
        "\n".join(out) + "\n",
        headers={"Content-Disposition": 'attachment; filename="all_statements.tsv"'},
    )


@app.delete("/api/statements/{sid}")
async def delete_statement(sid: str):
    STATEMENTS.pop(sid, None)
    (UPLOAD_DIR / f"{sid}.pdf").unlink(missing_ok=True)
    return {"ok": True}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
