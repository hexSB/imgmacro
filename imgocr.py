"""imgocr: find text on screen with the OCR engine built into Windows 10 and 11.

Only text rules use this file, and it's the only one that needs more than numpy,
opencv-python and mss: Microsoft's Python bindings for the Windows Runtime
(`pip install -r requirements-ocr.txt`). Without them, everything else works and
text rules say what's missing. Recognition runs on this computer, in the
languages installed in Windows (Settings → Time & language → Language).
"""

import asyncio
import difflib
import re
import threading

import cv2
import numpy as np

import imgmacro as im

INSTALL = "pip install -r requirements-ocr.txt"
_engine = None
_lock = threading.Lock()  # one recognition at a time; the engine isn't shared between threads


def _get_engine():
    global _engine
    if _engine is None:
        try:
            from winrt.windows.media.ocr import OcrEngine
        except ImportError:
            raise ValueError(f"text detection needs Windows' OCR packages ({INSTALL})") from None
        engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            raise ValueError("Windows has no OCR language installed (add one under Settings → "
                             "Time & language → Language)")
        _engine = engine
    return _engine


def available():
    """None if text detection works here, else why not."""
    try:
        _get_engine()
    except ValueError as exc:
        return str(exc)
    return None


def language():
    """The language the engine reads, e.g. 'en-US'."""
    return _get_engine().recognizer_language.language_tag


def read_lines(image):
    """The text in a BGR image -> [[(word, (x, y, w, h)), ...] per line], boxes in image pixels."""
    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import DataWriter

    from winrt.windows.media.ocr import OcrEngine

    engine = _get_engine()
    limit = OcrEngine.max_image_dimension
    h, w = image.shape[:2]
    # The engine reads small text badly; game UI text is often small, so enlarge small areas.
    scale = 1.0
    if max(h, w) * 2 <= limit and min(h, w) < 400:
        scale = 2.0 if min(h, w) >= 40 else 3.0
    scale = min(scale, limit / max(h, w, 1))
    if scale != 1.0:
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    bgra = np.ascontiguousarray(cv2.cvtColor(image, cv2.COLOR_BGR2BGRA))
    writer = DataWriter()
    writer.write_bytes(bgra.tobytes())
    bitmap = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8,
                                                    bgra.shape[1], bgra.shape[0],
                                                    BitmapAlphaMode.PREMULTIPLIED)

    async def recognize():
        return await engine.recognize_async(bitmap)

    with _lock:
        result = asyncio.run(recognize())
    lines = []
    for line in result.lines:
        words = []
        for word in line.words:
            r = word.bounding_rect
            words.append((word.text, (round(r.x / scale), round(r.y / scale),
                                      max(round(r.width / scale), 1), max(round(r.height / scale), 1))))
        if words:
            lines.append(words)
    return lines


def normalise(text):
    """For comparing: any case, and only letters and digits (OCR is unreliable about
    spaces and punctuation)."""
    return re.sub(r"[\W_]+", "", text.casefold())


def find_text(want, frame, origin, region=None):
    """Where `want` is written in `region` of a frame -> im.Hit, or None if the region is empty.

    The score is 1.0 when a line contains the text exactly (ignoring case, spaces
    and punctuation), else how close the closest run of words comes (0..1), so a
    misread letter or two still scores high. The centre is the middle of those words.
    """
    area, (ax, ay) = im.crop_area(frame, origin, region)
    if area.size == 0:
        return None
    target = normalise(want)
    h, w = area.shape[:2]
    best = im.Hit(0.0, ax + w // 2, ay + h // 2, (ax, ay, w, h), "no text read")
    if not target:
        return best
    for words in read_lines(area):
        norm = [normalise(word) for word, _ in words]
        line = "".join(norm)
        spans, at = [], 0  # where each word sits in `line`
        for n in norm:
            spans.append((at, at + len(n)))
            at += len(n)
        index = line.find(target)
        if index >= 0:
            used = [i for i, (a, b) in enumerate(spans) if a < index + len(target) and b > index]
            return _hit(1.0, [words[i][1] for i in used], ax, ay, " ".join(words[i][0] for i in used))
        for i in range(len(words)):
            for j in range(i + 1, len(words) + 1):
                phrase = "".join(norm[i:j])
                if len(phrase) > 2 * len(target):
                    break
                score = difflib.SequenceMatcher(None, phrase, target).ratio()
                if score > best.score:
                    best = _hit(score, [box for _, box in words[i:j]], ax, ay,
                                " ".join(word for word, _ in words[i:j]))
    return best


def _hit(score, boxes, ax, ay, read):
    left = min(x for x, _, _, _ in boxes)
    top = min(y for _, y, _, _ in boxes)
    right = max(x + w for x, _, w, _ in boxes)
    bottom = max(y + h for _, y, _, h in boxes)
    return im.Hit(score, ax + (left + right) // 2, ay + (top + bottom) // 2,
                  (ax + left, ay + top, right - left, bottom - top), f"read “{read}”")
