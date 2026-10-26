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

# Printed column rules. Some scans draw the table grid (Flathead rules every
# column), but as pixels, so pdfplumber's page.lines is empty and the column
# engine would otherwise have to infer boundaries from the header words alone.
#
# A rule is a *thin* vertical line: ink in a narrow strip with blank paper either
# side. Plain darkness per column does not work - USAble's black scan margins and
# Tapco's dense text both outscore Flathead's faint rules on it. So each x is
# scored by the fraction of page rows where [x-2, x+2] has ink while x-6 and x+6
# are both blank, which rejects solid regions and most text strokes alike.
#
# Measured over all 22 scans at 300 DPI:
#
#     Flathead's printed rules             0.32 - 0.87
#     highest score that is not a rule     0.23  (Liberty Mutual 181.82 text)
#
# 0.28 sits between. The strip is 5px wide so a slightly skewed scan still keeps
# its rule inside it. Re-measure before changing any of these.
RULE_INK = 160            # grey level below which a pixel counts as ink
RULE_HALF_WIDTH = 2       # px either side of x that the rule may wander through
RULE_CLEARANCE = 6        # px either side that must be blank paper
RULE_MIN_COVERAGE = 0.28

# Skew. Scans come off the feeder slightly tilted, and the engine groups words
# into lines within LINE_TOL = 3pt. FLATHEAD_SEPT's page 1 is tilted 0.55deg,
# which lifts the right end of a 560pt table row 5.4pt against its left end, so
# one printed row is split into several lines or fused with the next.
#
# The angle is whichever small rotation makes text rows sharpest - the variance
# of the per-row ink profile peaks when rows run level. Checked independently by
# cross-correlating the row profiles of the page's left and right halves:
#
#     FLATHEAD_SEPT p1   estimate  0.55   cross-check  0.54
#     FLATHEAD_SEPT p2            -0.65               -0.67
#     Heacock                     -0.40               -0.40
#
# `gain` is the sharpness at that angle over the sharpness unrotated. On a level
# page the landscape is flat and the angle is noise (Gateway reports 0.40deg at
# gain 1.03), so both must clear a bar. Measured over all 22 scans: pages that
# need it sit at |angle| 0.30-0.65 with gain 1.19-1.99; the rest at gain <=1.03
# or |angle| <=0.20, where drift stays under 2.2pt.
SKEW_LIMIT = 2.0          # degrees searched either side of level
SKEW_MIN_ANGLE = 0.25
SKEW_MIN_GAIN = 1.10
SKEW_SAMPLE_WIDTH = 1000  # px; the estimate runs on a downsampled copy

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


def skew_angle(image) -> tuple[float, float]:
    """(degrees, gain): the rotation that levels the text rows, and how much it helps.

    Rotate the image by +degrees (Pillow's counter-clockwise) to level it.
    Coarse 0.25deg sweep, then 0.05deg around the best, so a page costs ~30
    rotations of a small copy rather than ~80.
    """
    from PIL import Image

    grey = image.convert("L")
    height = round(grey.size[1] * SKEW_SAMPLE_WIDTH / grey.size[0])
    ink = grey.resize((SKEW_SAMPLE_WIDTH, height), Image.BOX).point(
        lambda v: 255 if v < RULE_INK else 0
    )

    def sharpness(angle: float) -> float:
        turned = ink.rotate(angle, resample=Image.BILINEAR, fillcolor=0)
        rows = list(turned.resize((1, turned.size[1]), Image.BOX).getdata())
        mean = sum(rows) / len(rows)
        return sum((v - mean) ** 2 for v in rows) / len(rows)

    scores: dict[float, float] = {}

    def sweep(centre: float, step: float, reach: float) -> float:
        n = round(reach / step)
        for i in range(-n, n + 1):
            angle = round(centre + i * step, 2)
            if abs(angle) <= SKEW_LIMIT and angle not in scores:
                scores[angle] = sharpness(angle)
        return max(scores, key=scores.get)

    best = sweep(sweep(0.0, 0.25, SKEW_LIMIT), 0.05, 0.25)
    level = scores.get(0.0) or 1e-9
    return best, scores[best] / level


def column_rules(image, scale: float) -> list[float]:
    """x positions, in points, of the printed vertical rules on a page image.

    Pillow only - ImageChops shifts do the neighbour tests and a one-pixel-tall
    BOX resize averages every column, so this needs no array library.
    """
    from PIL import Image, ImageChops

    ink = image.convert("L").point(lambda v: 255 if v < RULE_INK else 0)
    strip = ink
    for d in range(1, RULE_HALF_WIDTH + 1):
        strip = ImageChops.lighter(strip, ImageChops.offset(ink, d, 0))
        strip = ImageChops.lighter(strip, ImageChops.offset(ink, -d, 0))
    either_side = ImageChops.lighter(
        ImageChops.offset(ink, RULE_CLEARANCE, 0),
        ImageChops.offset(ink, -RULE_CLEARANCE, 0),
    )
    thin = ImageChops.darker(strip, ImageChops.invert(either_side))
    width = thin.size[0]
    coverage = [v / 255 for v in thin.resize((width, 1), Image.BOX).getdata()]

    # Offsets wrap around the image, so the outermost columns are not trusted.
    margin = RULE_CLEARANCE + RULE_HALF_WIDTH
    hits = [
        x for x in range(margin, width - margin)
        if coverage[x] >= RULE_MIN_COVERAGE
    ]

    # One rule spans a few adjacent columns, and a skewed one can break into two
    # runs (Flathead page 2: 10px apart). Hits closer than 2 x RULE_CLEARANCE are
    # one rule - a gap that narrow cannot hold a character (a 7pt glyph is ~17px
    # at 300 DPI) so it cannot be a column. Each rule keeps its strongest x.
    rules: list[float] = []
    run: list[int] = []
    for x in hits:
        if run and x - run[-1] >= 2 * RULE_CLEARANCE:
            rules.append(max(run, key=lambda i: coverage[i]) * scale)
            run = []
        run.append(x)
    if run:
        rules.append(max(run, key=lambda i: coverage[i]) * scale)
    return rules


def page_words(page) -> tuple[list[dict], str, list[float]]:
    """OCR one pdfplumber page into engine word dicts.

    Returns (words, note, rules). `note` is a message for stmt.warnings and is
    empty on a clean read; words is empty if OCR is unavailable or found nothing.
    `rules` are the printed vertical rules (see column_rules), measured on the
    same rotated image the words came from so both share one coordinate space.
    """
    ok, reason = available()
    if not ok:
        return [], reason, []

    from PIL import Image

    pytesseract = _configured()
    scale = PDF_POINTS_PER_INCH / RENDER_DPI

    try:
        image = page.to_image(resolution=RENDER_DPI).original
    except Exception as exc:
        return [], f"OCR could not rasterise this page: {exc}", []

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

    best = None
    for angle in order:
        candidate = image if angle == 0 else image.rotate(-angle, expand=True)
        try:
            data = pytesseract.image_to_data(
                candidate,
                config=TESSERACT_CONFIG,
                output_type=pytesseract.Output.DICT,
            )
        except Exception as exc:
            return [], f"OCR failed on this page: {exc}", []

        words, mean, dropped = _words(data, scale)
        score = (mean, len(words))
        if best is None or score > best[:2]:
            best = (mean, len(words), words, dropped, angle, candidate)
        if mean >= GOOD_MEAN_CONF and len(words) >= GOOD_WORD_COUNT:
            break

    if best is None:
        return [], "OCR produced no usable text on this page.", []

    mean, count, words, dropped, angle, upright = best
    if not words:
        return [], "OCR found no legible text on this page.", []

    # Level the page only after its orientation is settled: the skew estimate
    # reads text rows, which are only horizontal on the upright image. One extra
    # OCR pass, and only on pages that are measurably tilted.
    tilt, gain = skew_angle(upright)
    levelled = 0.0
    if abs(tilt) >= SKEW_MIN_ANGLE and gain >= SKEW_MIN_GAIN:
        straight = upright.rotate(
            tilt, resample=Image.BICUBIC, fillcolor="white"
        )
        try:
            data = pytesseract.image_to_data(
                straight,
                config=TESSERACT_CONFIG,
                output_type=pytesseract.Output.DICT,
            )
            redone, redone_mean, redone_dropped = _words(data, scale)
        except Exception:
            redone = []
        if redone:
            words, mean, dropped, count = (
                redone, redone_mean, redone_dropped, len(redone)
            )
            upright, levelled = straight, tilt

    rules = column_rules(upright, scale)
    note = (
        f"Read by OCR at {RENDER_DPI} DPI"
        + (f", rotated {angle}deg" if angle else "")
        + (f", levelled {levelled:+.2f}deg" if levelled else "")
        + f" - {count} words at {mean:.0f}% mean confidence"
        + (f", {dropped} below {MIN_CONF}% discarded" if dropped else "")
        + (f", {len(rules)} printed column rules found" if rules else "")
        + ". OCR misreads digits, so check the figures against the statement."
    )
    return words, note, rules
