"""Measure OCR settings against the image-only scans in statements/.

The third report script, alongside carriers_report.py and failsafe_report.py. Run
this before changing RENDER_DPI, TESSERACT_CONFIG or MIN_CONF in app/ocr.py, and
quote the numbers - those constants were chosen from this sweep, not preference.

Scoring uses the amount in the filename as ground truth: a setting is better if
the OCR text actually contains the figure the statement is known to pay. That is
an objective target available for every file, unlike eyeballing the text.

    python scripts/ocr_report.py              # sweep page-segmentation modes
    python scripts/ocr_report.py --dpi        # sweep render resolution instead

Note this scores whether OCR *saw* the amount, which is necessary but not
sufficient - the column engine still has to put it in the right column, and the
failsafe still has to reconcile it. Use failsafe_report.py for the end-to-end
verdict.
"""

from __future__ import annotations

import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pdfplumber  # noqa: E402

from app import ocr  # noqa: E402
from app.extract import filename_amount  # noqa: E402

PSM_MODES = [3, 4, 6, 11, 12]
DPI_STEPS = [200, 300, 400]
MAX_PAGES = 3          # keeps a 7-page scan from dominating the runtime


def scans() -> list[str]:
    """Every PDF in statements/ with no text layer - the OCR cases."""
    out = []
    for path in sorted(set(glob.glob("statements/*.pdf") + glob.glob("statements/*.PDF"))):
        try:
            with pdfplumber.open(path) as pdf:
                if pdf.pages and not any(p.chars for p in pdf.pages):
                    out.append(path)
        except Exception:
            continue       # unopenable (ISC reports 0 pages) - not an OCR case
    return out


def _upright(images, pytesseract):
    """Apply one OSD rotation to a file's pages, so the sweep compares settings
    rather than re-deciding orientation for every candidate."""
    try:
        osd = pytesseract.image_to_osd(images[0], output_type=pytesseract.Output.DICT)
        angle = int(osd.get("rotate", 0)) % 360
    except Exception:
        angle = 0
    if not angle:
        return images, angle
    return [im.rotate(-angle, expand=True) for im in images], angle


def _read(image, pytesseract, psm: int) -> tuple[int, float, set[str]]:
    data = pytesseract.image_to_data(
        image, config=f"--psm {psm}", output_type=pytesseract.Output.DICT
    )
    texts, confs = set(), []
    for i, raw in enumerate(data["text"]):
        text = (raw or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            continue
        if conf < ocr.MIN_CONF:
            continue
        texts.add(text.replace(",", ""))
        confs.append(conf)
    return len(texts), (sum(confs) / len(confs) if confs else 0.0), texts


def main() -> None:
    ok, reason = ocr.available()
    if not ok:
        print(reason)
        raise SystemExit(1)

    pytesseract = ocr._configured()
    by_dpi = "--dpi" in sys.argv
    settings = DPI_STEPS if by_dpi else PSM_MODES
    label = "dpi" if by_dpi else "psm"

    files = scans()
    if not files:
        print("No image-only scans found in statements/.")
        return
    print(f"{len(files)} image-only scans · sweeping {label} {settings}\n")

    hits = {s: 0 for s in settings}
    scored = 0

    for path in files:
        name = os.path.basename(path)
        target = filename_amount(name)
        want = f"{abs(target):.2f}" if target is not None else None
        if want:
            scored += 1

        cells = []
        for setting in settings:
            dpi = setting if by_dpi else ocr.RENDER_DPI
            psm = 6 if by_dpi else setting
            try:
                with pdfplumber.open(path) as pdf:
                    images = [
                        p.to_image(resolution=dpi).original for p in pdf.pages[:MAX_PAGES]
                    ]
                images, angle = _upright(images, pytesseract)
                words, conf, texts = 0, [], set()
                for image in images:
                    w, c, t = _read(image, pytesseract, psm)
                    words += w
                    conf.append(c)
                    texts |= t
                found = bool(want) and any(want in t for t in texts)
                if found:
                    hits[setting] += 1
                mean = sum(conf) / len(conf) if conf else 0.0
                cells.append(f"{label}{setting}:{'Y' if found else '.'}({words}w/{mean:.0f}c)")
            except Exception as exc:
                cells.append(f"{label}{setting}:ERR({type(exc).__name__})")

        print(f"{name:32} want={want or '-':>9}  " + "  ".join(cells), flush=True)

    print(f"\nfilename amount located (of {scored} files with an amount):")
    for setting in settings:
        print(f"  {label} {setting:4}: {hits[setting]}/{scored}")
    print(
        "\nA higher score means OCR saw the figure. It does not mean the table "
        "parsed correctly - run scripts/failsafe_report.py for that."
    )


if __name__ == "__main__":
    main()
