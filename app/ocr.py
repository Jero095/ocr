"""OCR tier for image-only scans.

16 carriers ship statements as scanned images with no text layer, so the geometry
engine had nothing to read and they reported "needs OCR" and stopped. This module
is that OCR.

The important thing it does *not* do is try to understand tables. Tesseract's
table handling is weak, but the engine in extract.py already turns word boxes into
columns - that is its whole job - so all this module has to produce is the same
word dicts pdfplumber's extract_words() produces:

    {"text": str, "x0": float, "x1": float, "top": float, "bottom": float}

Everything downstream (band detection, max-overlap column assignment, the totals
row, the failsafe) then works unchanged on a scan.

Two things needed real care:

**Coordinates must be in PDF points, not pixels.** LINE_TOL, BAND_GAP and
MERGE_GAP in extract.py are measured constants in points. Handing the engine pixel
coordinates at 300 DPI would inflate every gap by 4.167x and silently break every
one of them, so pixels are scaled back to points here.

**Orientation is measured, not trusted.** These scans are frequently sideways -
Heacock's is stored as a portrait page containing landscape content. Tesseract's
OSD detects that, but reported only 13.83% confidence on the very page it got
right, so OSD is used as a *hint* and the choice is settled by which rotation
actually yields confident words. Same principle as the auto-detector: the
measurement decides.

OCR misreads digits (5/S, 8/B, 0/O). That is exactly what the failsafe already
guards - the extracted commission column is reconciled against the amount in the
filename - so a bad read gets flagged rather than exported. Do not weaken that
check to make scans pass.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

RENDER_DPI = 300          # 4.167x the PDF's 72pt grid; below ~200 accuracy falls off
PDF_POINTS_PER_INCH = 72

# Page segmentation mode 6 ("assume a single uniform block of text"), NOT
# Tesseract's default 3 ("fully automatic with page segmentation"). Measured, not
# preferred: these scans carry vertical fold and crease lines, and PSM 3's layout
# analysis reads them as column boundaries and discards whole regions. On
# Heacock it found the check amount but neither of the two $16.44 figures in the
# Amount Paid column at the right edge.
#
# Swept all 21 readable scans, scoring each mode on whether the amount in the
# filename appears anywhere in the OCR output - the filenames are ground truth:
#
#     psm 3 (default)  10/21        psm 6   12/21
#     psm 4            10/21        psm 11  12/21
#
# 6 and 11 tie on that score; 6 returns more words per page (Austin Mutual: 303
# vs 234), and word density is what the column engine needs. Re-run the sweep in
# scripts/ocr_report.py before changing this.
TESSERACT_CONFIG = "--psm 6"

# Tesseract reports -1 for boxes it found no text in, and single-digit scores for
# noise. Kept deliberately low: a wrong figure is caught by the failsafe, whereas
# a *dropped* figure silently shrinks the total.
MIN_CONF = 25

# One rotation is accepted without trying the rest when it looks this good, so the
# common case costs a single OCR pass rather than four.
GOOD_MEAN_CONF = 75.0
GOOD_WORD_COUNT = 15

_WINDOWS_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)


def _find_tesseract() -> str | None:
    """Locate the Tesseract binary.

    The Windows installer does not always put it on PATH for an already-running
    shell, so the standard install locations are checked too.
    """
    found = shutil.which("tesseract")
    if found:
        return found
    env = os.environ.get("TESSERACT_CMD")
    if env and Path(env).exists():
        return env
    for candidate in _WINDOWS_PATHS:
        if Path(candidate).exists():
            return candidate
    return None


def available() -> tuple[bool, str]:
    """(usable, reason) - reason is empty when usable, else a message for the UI."""
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        return False, (
            "OCR is not installed: the pytesseract package is missing. "
            "Run: pip install -r requirements.txt"
        )
    binary = _find_tesseract()
    if not binary:
        return False, (
            "OCR is not installed: the Tesseract engine was not found. "
            "Install it with: winget install UB-Mannheim.TesseractOCR"
        )
    return True, ""


def _configured():
    """Import pytesseract with tesseract_cmd pointed at the binary we found."""
    import pytesseract

    binary = _find_tesseract()
    if binary:
        pytesseract.pytesseract.tesseract_cmd = binary
    return pytesseract


def _words(data: dict, scale: float) -> tuple[list[dict], float, int]:
    """Convert one image_to_data result into engine word dicts, in points.

    Returns (words, mean_confidence, dropped) so the caller can compare rotations
    on measured quality rather than assuming Tesseract's own orientation guess.
    """
    words: list[dict] = []
    confidences: list[float] = []
    dropped = 0

    for i, text in enumerate(data["text"]):
        text = (text or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if conf < MIN_CONF:
            dropped += 1
            continue
        left, top = data["left"][i], data["top"][i]
        width, height = data["width"][i], data["height"][i]
        words.append(
            {
                "text": text,
                "x0": left * scale,
                "x1": (left + width) * scale,
                "top": top * scale,
                "bottom": (top + height) * scale,
            }
        )
        confidences.append(conf)

    mean = sum(confidences) / len(confidences) if confidences else 0.0
    return words, mean, dropped


def page_words(page) -> tuple[list[dict], str]:
    """OCR one pdfplumber page into engine word dicts.

    Returns (words, note). `note` is a message for stmt.warnings and is empty on a
    clean read; words is empty if OCR is unavailable or found nothing.
    """
    ok, reason = available()
    if not ok:
        return [], reason

    pytesseract = _configured()
    scale = PDF_POINTS_PER_INCH / RENDER_DPI

    try:
        image = page.to_image(resolution=RENDER_DPI).original
    except Exception as exc:
        return [], f"OCR could not rasterise this page: {exc}"

    # OSD first as a hint, then the remaining rotations. Its confidence is
    # unreliable (13.83% on a page it read correctly), so the winner is whichever
    # rotation produces the most confident words, not whichever OSD names.
    order: list[int] = []
    try:
        osd = pytesseract.image_to_osd(image, output_type=pytesseract.Output.DICT)
        order.append(int(osd.get("rotate", 0)) % 360)
    except Exception:
        pass
    order += [a for a in (0, 90, 180, 270) if a not in order]

    best: tuple[float, int, list[dict], int, int] | None = None
    for angle in order:
        candidate = image if angle == 0 else image.rotate(-angle, expand=True)
        try:
            data = pytesseract.image_to_data(
                candidate,
                config=TESSERACT_CONFIG,
                output_type=pytesseract.Output.DICT,
            )
        except Exception as exc:
            return [], f"OCR failed on this page: {exc}"

        words, mean, dropped = _words(data, scale)
        score = (mean, len(words))
        if best is None or score > best[:2]:
            best = (mean, len(words), words, dropped, angle)
        if mean >= GOOD_MEAN_CONF and len(words) >= GOOD_WORD_COUNT:
            break

    if best is None:
        return [], "OCR produced no usable text on this page."

    mean, count, words, dropped, angle = best
    if not words:
        return [], "OCR found no legible text on this page."

    note = (
        f"Read by OCR at {RENDER_DPI} DPI"
        + (f", rotated {angle}deg" if angle else "")
        + f" - {count} words at {mean:.0f}% mean confidence"
        + (f", {dropped} below {MIN_CONF}% discarded" if dropped else "")
        + ". OCR misreads digits, so check the figures against the statement."
    )
    return words, note
