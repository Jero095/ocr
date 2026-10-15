"""Simple OCR: free text out of a single image.

Separate from app/ocr.py on purpose. That module feeds word *boxes* to the table
engine and is tuned against the carrier scans; this one returns plain text for a
person to read and copy, and is tuned for photos and handwriting. Sharing code
would couple two sets of measured constants that answer different questions.

Two engines:

- **claude** - Claude's vision model. The only option that reads cursive
  handwriting reliably, and it copes with any rotation without being told. The
  image is sent to the Anthropic API, so it needs ANTHROPIC_API_KEY (or an
  `ant auth login` profile) and it means the image leaves this machine.
- **tesseract** - local, free, nothing leaves the box. Good on print, poor on
  handwriting. Rotation is chosen the same way app/ocr.py does it: OSD as a hint,
  the most confident rotation wins.

Nothing here is persisted. The image is read, transcribed and dropped.
"""

from __future__ import annotations

import base64
import io
import os

from PIL import Image, ImageOps

from . import ocr as statement_ocr

# code -> (label, tesseract language string). "auto" has no single Tesseract
# equivalent, so it runs every installed Latin-script pack together.
LANGUAGES: dict[str, tuple[str, str]] = {
    "auto": ("Auto-detect", ""),
    "en": ("English", "eng"),
    "fr": ("French", "fra"),
    "es": ("Spanish", "spa"),
    "pt": ("Portuguese", "por"),
    "it": ("Italian", "ita"),
    "de": ("German", "deu"),
}

CLAUDE_MODEL = "claude-opus-5-5"

# The API downsamples anything beyond ~2000px on the long edge anyway, and caps a
# base64 image at 5MB. Shrinking here keeps phone photos under that cap instead
# of failing with a 400.
CLAUDE_MAX_EDGE = 2000
CLAUDE_MAX_BYTES = 5 * 1024 * 1024

# Below this, Tesseract's character models lose stroke detail. Small screenshots
# are upscaled to roughly this many pixels on the long edge.
TESSERACT_MIN_EDGE = 1800

# Same meaning as in app/ocr.py: accept a rotation without trying the rest when it
# already looks this good.
GOOD_MEAN_CONF = 75.0

# PSM 6, not the default 3, and for a different reason than app/ocr.py. PSM 3's
# layout analysis reads a 90deg-sideways block by itself, so every rotation scores
# similar confidence and the rotation chooser picked wrong (an upright page came
# back "rotated 90", a 90deg page "rotated 180"). PSM 6 reads only what is upright,
# so confidence separates the angles cleanly. Measured on a handwritten French
# page in all four orientations: the right angle scored 52 mean confidence every
# time, the best wrong one 35. It also reads better: 7 of 15 keywords vs 4.
TESSERACT_CONFIG = "--psm 6"

SYSTEM_PROMPT = """You transcribe text from images exactly as written.

- Output only the transcribed text. No preamble, no commentary, no markdown fences.
- The image may be rotated or photographed at an angle; read it in its natural orientation.
- Preserve the original line breaks, spelling, accents and punctuation. Do not correct, translate or summarise.
- If a word is genuinely illegible, write [illegible] in its place rather than guessing.
- If the image contains no text, output exactly: [no text found]"""


class OCRError(Exception):
    """A failure worth showing to the user verbatim."""


def languages() -> list[dict]:
    return [{"code": code, "label": label} for code, (label, _) in LANGUAGES.items()]


def engines() -> list[dict]:
    """Which engines can run right now, and why not when they cannot."""
    claude_ok, claude_reason = _claude_available()
    tess_ok, tess_reason = statement_ocr.available()
    return [
        {"id": "claude", "label": "Claude (handwriting)", "available": claude_ok,
         "reason": claude_reason, "local": False},
        {"id": "tesseract", "label": "Local (Tesseract)", "available": tess_ok,
         "reason": tess_reason, "local": True},
    ]


def _open(data: bytes) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:
        raise OCRError("That file is not an image this app can read.") from exc
    # Phone photos are often stored sideways with an EXIF flag saying so.
    return ImageOps.exif_transpose(image)


def transcribe(data: bytes, engine: str, language: str) -> dict:
    if language not in LANGUAGES:
        raise OCRError(f"Unknown language: {language}")
    image = _open(data)
    if engine == "claude":
        return _claude(image, language)
    if engine == "tesseract":
        return _tesseract(image, language)
    raise OCRError(f"Unknown engine: {engine}")


# --- Claude ------------------------------------------------------------------

def _claude_available() -> tuple[bool, str]:
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False, ("The anthropic package is missing. "
                       "Run: pip install -r requirements.txt")
    # The SDK also resolves `ant auth login` profiles, so a missing env var is
    # not proof of missing credentials; only an env var is checked cheaply here,
    # and the request itself reports anything else.
    if not (os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or os.environ.get("ANTHROPIC_PROFILE")):
        return False, ("No Anthropic API key. Set ANTHROPIC_API_KEY and "
                       "restart the server.")
    return True, ""


def _encode_for_claude(image: Image.Image) -> tuple[str, str]:
    image = image.convert("RGB")
    image.thumbnail((CLAUDE_MAX_EDGE, CLAUDE_MAX_EDGE))
    for quality in (92, 85, 75):
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=quality)
        encoded = base64.standard_b64encode(buf.getvalue()).decode("ascii")
        if len(encoded) <= CLAUDE_MAX_BYTES:
            return encoded, "image/jpeg"
    raise OCRError("That image is too large to send, even after compression.")


def _claude(image: Image.Image, language: str) -> dict:
    ok, reason = _claude_available()
    if not ok:
        raise OCRError(reason)
    import anthropic

    data, media_type = _encode_for_claude(image)
    label = LANGUAGES[language][0]
    instruction = ("Transcribe all the text in this image."
                   if language == "auto"
                   else f"Transcribe all the text in this image. It is written in {label}.")

    client = anthropic.Anthropic()
    try:
        response = client.beta.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            # If a safety classifier declines, the API retries on a fallback
            # model within the same call instead of returning nothing.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image",
                     "source": {"type": "base64", "media_type": media_type, "data": data}},
                    {"type": "text", "text": instruction},
                ],
            }],
        )
    except anthropic.AuthenticationError as exc:
        raise OCRError("The Anthropic API key was rejected.") from exc
    except anthropic.PermissionDeniedError as exc:
        raise OCRError("The Anthropic API key lacks permission for this model.") from exc
    except anthropic.RateLimitError as exc:
        raise OCRError("Rate limited by the Anthropic API. Try again shortly.") from exc
    except anthropic.APIStatusError as exc:
        raise OCRError(f"Anthropic API error ({exc.status_code}): {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise OCRError("Could not reach the Anthropic API.") from exc

    if response.stop_reason == "refusal":
        raise OCRError("The model declined to transcribe this image.")
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    note = f"Read by {response.model}"
    if response.stop_reason == "max_tokens":
        note += " - output was cut off at the length limit"
    return {"text": text, "engine": "claude", "note": note + "."}


# --- Tesseract ---------------------------------------------------------------

def _tesseract_lang(pytesseract, language: str) -> str:
    try:
        installed = set(pytesseract.get_languages(config=""))
    except Exception:
        installed = {"eng"}
    if language == "auto":
        wanted = [code for _, code in LANGUAGES.values() if code in installed]
        return "+".join(wanted) or "eng"
    code = LANGUAGES[language][1]
    if code not in installed:
        raise OCRError(
            f"The Tesseract {LANGUAGES[language][0]} pack ({code}) is not "
            f"installed. Add {code}.traineddata to the tessdata folder, or use "
            f"the Claude engine."
        )
    return code


def _prepare(image: Image.Image) -> Image.Image:
    gray = ImageOps.grayscale(image)
    long_edge = max(gray.size)
    if long_edge < TESSERACT_MIN_EDGE:
        factor = TESSERACT_MIN_EDGE / long_edge
        gray = gray.resize((round(gray.width * factor), round(gray.height * factor)),
                           Image.LANCZOS)
    return ImageOps.autocontrast(gray)


def _mean_conf(data: dict) -> tuple[float, int]:
    confs = []
    for text, conf in zip(data["text"], data["conf"]):
        try:
            value = float(conf)
        except (TypeError, ValueError):
            continue
        if (text or "").strip() and value >= 0:
            confs.append(value)
    return (sum(confs) / len(confs) if confs else 0.0), len(confs)


def _tesseract(image: Image.Image, language: str) -> dict:
    ok, reason = statement_ocr.available()
    if not ok:
        raise OCRError(reason)
    pytesseract = statement_ocr._configured()
    lang = _tesseract_lang(pytesseract, language)
    image = _prepare(image)

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
        data = pytesseract.image_to_data(candidate, lang=lang, config=TESSERACT_CONFIG,
                                         output_type=pytesseract.Output.DICT)
        mean, count = _mean_conf(data)
        if best is None or (mean, count) > best[:2]:
            best = (mean, count, angle, candidate)
        if mean >= GOOD_MEAN_CONF:
            break

    mean, count, angle, candidate = best
    text = pytesseract.image_to_string(candidate, lang=lang,
                                       config=TESSERACT_CONFIG).strip()
    note = (f"Read locally by Tesseract ({lang})"
            + (f", rotated {angle}deg" if angle else "")
            + f" - {count} words at {mean:.0f}% mean confidence."
            + (" Tesseract is weak on handwriting; try the Claude engine."
               if mean < 70 else ""))
    return {"text": text, "engine": "tesseract", "note": note}
