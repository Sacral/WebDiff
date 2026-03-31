#!/usr/bin/env python3
"""
Compare two offline site snapshots and generate a visual HTML diff report.

Features
- Text diff for html/js/css/json/txt/xml/md files
- Image diff with optional pixel highlight output (requires Pillow)
- Added/removed/modified file summary
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import collections
import datetime as dt
import difflib
import hashlib
import html
import json
import shutil
import sys
import time
import urllib.parse
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from functools import partial
from threading import Thread
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat

    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import async_playwright

    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


TEXT_EXTENSIONS: Set[str] = {
    ".html",
    ".htm",
    ".css",
    ".js",
    ".json",
    ".txt",
    ".xml",
    ".md",
    ".svg",
}

IMAGE_EXTENSIONS: Set[str] = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
}


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as f:
        while True:
            block = f.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def sniff_image_kind(path: Path) -> Optional[str]:
    try:
        with path.open("rb") as f:
            header = f.read(16)
    except OSError:
        return None

    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if header.startswith(b"RIFF") and len(header) >= 12 and header[8:12] == b"WEBP":
        return "webp"
    if header.startswith(b"BM"):
        return "bmp"
    return None


def is_text_file(path: Path) -> bool:
    if path.suffix.lower() in TEXT_EXTENSIONS:
        return True
    mime_guess = sniff_image_kind(path)
    return mime_guess is None and path.suffix.lower() not in IMAGE_EXTENSIONS


def is_image_file(path: Path) -> bool:
    if path.suffix.lower() in IMAGE_EXTENSIONS:
        return True
    return sniff_image_kind(path) is not None


def collect_files(root: Path) -> Dict[str, Path]:
    file_map: Dict[str, Path] = {}
    for p in root.rglob("*"):
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            file_map[rel] = p
    return file_map


def read_text_lines(path: Path) -> List[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="ignore")
    return text.splitlines()


def make_unified_diff(old_file: Path, new_file: Path, rel: str, context_lines: int) -> str:
    old_lines = read_text_lines(old_file)
    new_lines = read_text_lines(new_file)
    diff_lines = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"old/{rel}",
        tofile=f"new/{rel}",
        n=context_lines,
        lineterm="",
    )
    return "\n".join(diff_lines)


def _highlight_line_diff(old_text: str, new_text: str) -> Tuple[str, str]:
    matcher = difflib.SequenceMatcher(a=old_text, b=new_text)
    old_parts: List[str] = []
    new_parts: List[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        old_seg = html.escape(old_text[i1:i2])
        new_seg = html.escape(new_text[j1:j2])
        if tag == "equal":
            old_parts.append(old_seg)
            new_parts.append(new_seg)
        elif tag == "delete":
            old_parts.append(f"<span class='hl-del'>{old_seg}</span>")
        elif tag == "insert":
            new_parts.append(f"<span class='hl-ins'>{new_seg}</span>")
        else:
            old_parts.append(f"<span class='hl-del'>{old_seg}</span>")
            new_parts.append(f"<span class='hl-ins'>{new_seg}</span>")
    return "".join(old_parts), "".join(new_parts)


def build_side_by_side_rows(
    old_file: Path,
    new_file: Path,
    context_lines: int,
    max_rows: int,
) -> Dict[str, object]:
    old_lines = read_text_lines(old_file)
    new_lines = read_text_lines(new_file)
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines)

    raw_rows: List[Dict[str, object]] = []
    old_no = 1
    new_no = 1
    changed_count = {"insert": 0, "delete": 0, "replace": 0}

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for _ in range(i2 - i1):
                raw_rows.append(
                    {
                        "tag": "equal",
                        "old_no": old_no,
                        "new_no": new_no,
                        "old_text": old_lines[old_no - 1],
                        "new_text": new_lines[new_no - 1],
                    }
                )
                old_no += 1
                new_no += 1
            continue

        if tag == "replace":
            changed_count["replace"] += max(i2 - i1, j2 - j1)
        elif tag == "delete":
            changed_count["delete"] += i2 - i1
        elif tag == "insert":
            changed_count["insert"] += j2 - j1

        old_chunk = old_lines[i1:i2]
        new_chunk = new_lines[j1:j2]
        span = max(len(old_chunk), len(new_chunk))
        for k in range(span):
            old_text = old_chunk[k] if k < len(old_chunk) else ""
            new_text = new_chunk[k] if k < len(new_chunk) else ""
            row_tag = tag
            if tag == "replace" and old_text == "" and new_text != "":
                row_tag = "insert"
            elif tag == "replace" and old_text != "" and new_text == "":
                row_tag = "delete"
            raw_rows.append(
                {
                    "tag": row_tag,
                    "old_no": old_no if old_text != "" else None,
                    "new_no": new_no if new_text != "" else None,
                    "old_text": old_text,
                    "new_text": new_text,
                }
            )
            if old_text != "":
                old_no += 1
            if new_text != "":
                new_no += 1

    changed_indices = [idx for idx, r in enumerate(raw_rows) if r["tag"] != "equal"]
    if not changed_indices:
        return {
            "rows": [],
            "truncated": False,
            "change_stats": changed_count,
        }

    keep = set()
    for idx in changed_indices:
        left = max(0, idx - max(0, context_lines))
        right = min(len(raw_rows), idx + max(0, context_lines) + 1)
        keep.update(range(left, right))

    ordered_keep = sorted(keep)
    filtered_rows: List[Dict[str, object]] = []
    prev_idx: Optional[int] = None
    for idx in ordered_keep:
        if prev_idx is not None and idx - prev_idx > 1:
            filtered_rows.append(
                {
                    "tag": "skip",
                    "old_no": None,
                    "new_no": None,
                    "old_text": f"... 略過 {idx - prev_idx - 1} 行相同內容 ...",
                    "new_text": f"... 略過 {idx - prev_idx - 1} 行相同內容 ...",
                }
            )
        filtered_rows.append(raw_rows[idx])
        prev_idx = idx

    truncated = False
    if len(filtered_rows) > max_rows:
        filtered_rows = filtered_rows[:max_rows]
        filtered_rows.append(
            {
                "tag": "skip",
                "old_no": None,
                "new_no": None,
                "old_text": "... 已截斷更多異動內容 ...",
                "new_text": "... 已截斷更多異動內容 ...",
            }
        )
        truncated = True

    return {
        "rows": filtered_rows,
        "truncated": truncated,
        "change_stats": changed_count,
    }


def _save_image_preview(image_path: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(image_path, output_path)


def _crop_trailing_whitespace(
    img: "Image.Image",
    bg_threshold: int = 245,
    min_height: int = 200,
    bottom_pad: int = 30,
) -> "Image.Image":
    """Crop near-white rows from the bottom of a screenshot.

    Samples every 8th pixel per row for performance.  Rows where every sampled
    pixel is lighter than *bg_threshold* (grayscale) are considered blank.
    At least *min_height* pixels are always kept, and *bottom_pad* pixels of
    padding are added below the last detected content row.
    """
    if not PIL_AVAILABLE:
        return img
    w, h = img.size
    if h <= min_height:
        return img
    gray = img.convert("L")
    pixels = gray.load()
    step = max(1, w // 128)  # ~128 sample points per row
    last_content = min_height - 1
    for y in range(h - 1, min_height - 1, -1):
        if any(pixels[x, y] < bg_threshold for x in range(0, w, step)):
            last_content = y
            break
    new_h = min(h, last_content + 1 + bottom_pad)
    if new_h >= h - 10:
        return img
    return img.crop((0, 0, w, new_h))


def _build_diff_boxes(mask: "Image.Image") -> List[Tuple[int, int, int, int]]:
    width, height = mask.size
    if width <= 0 or height <= 0:
        return []

    # Smooth tiny gaps/noise first so connected components are more stable.
    normalized = mask.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3))
    pixels = normalized.load()
    visited = bytearray(width * height)
    boxes: List[Tuple[int, int, int, int]] = []
    min_pixels = 50   # ignore tiny noise clusters (< 50 changed pixels)
    padding = 3

    for y in range(height):
        row_base = y * width
        for x in range(width):
            idx = row_base + x
            if visited[idx] or pixels[x, y] == 0:
                continue

            queue = collections.deque([(x, y)])
            visited[idx] = 1
            min_x = max_x = x
            min_y = max_y = y
            area = 0

            while queue:
                cx, cy = queue.popleft()
                area += 1
                min_x = min(min_x, cx)
                max_x = max(max_x, cx)
                min_y = min(min_y, cy)
                max_y = max(max_y, cy)

                for ny in (cy - 1, cy, cy + 1):
                    if ny < 0 or ny >= height:
                        continue
                    nrow = ny * width
                    for nx in (cx - 1, cx, cx + 1):
                        if nx < 0 or nx >= width or (nx == cx and ny == cy):
                            continue
                        nidx = nrow + nx
                        if visited[nidx] or pixels[nx, ny] == 0:
                            continue
                        visited[nidx] = 1
                        queue.append((nx, ny))

            if area < min_pixels:
                continue

            boxes.append(
                (
                    max(0, min_x - padding),
                    max(0, min_y - padding),
                    min(width, max_x + 1 + padding),
                    min(height, max_y + 1 + padding),
                )
            )

    # Merge overlapping/nearby boxes so one changed block gets one marker.
    merge_gap = 6
    merged = sorted(boxes)
    changed = True
    while changed:
        changed = False
        next_boxes: List[Tuple[int, int, int, int]] = []
        while merged:
            left, top, right, bottom = merged.pop(0)
            i = 0
            while i < len(merged):
                l2, t2, r2, b2 = merged[i]
                separated = (
                    right + merge_gap < l2
                    or r2 + merge_gap < left
                    or bottom + merge_gap < t2
                    or b2 + merge_gap < top
                )
                if separated:
                    i += 1
                    continue
                left = min(left, l2)
                top = min(top, t2)
                right = max(right, r2)
                bottom = max(bottom, b2)
                merged.pop(i)
                changed = True
            next_boxes.append((left, top, right, bottom))
        merged = sorted(next_boxes)

    return merged


def _merge_rect_list(
    rects: List[Tuple[int, int, int, int]],
    gap: int = 8,
) -> List[Tuple[int, int, int, int]]:
    """Merge a flat list of rectangles that overlap or are closer than *gap* pixels."""
    if not rects:
        return []
    merged = sorted(rects)
    changed = True
    while changed:
        changed = False
        next_rects: List[Tuple[int, int, int, int]] = []
        while merged:
            left, top, right, bottom = merged.pop(0)
            i = 0
            while i < len(merged):
                l2, t2, r2, b2 = merged[i]
                if right + gap >= l2 and r2 + gap >= left and bottom + gap >= t2 and b2 + gap >= top:
                    left = min(left, l2)
                    top = min(top, t2)
                    right = max(right, r2)
                    bottom = max(bottom, b2)
                    merged.pop(i)
                    changed = True
                else:
                    i += 1
            next_rects.append((left, top, right, bottom))
        merged = sorted(next_rects)
    return merged


def _expand_to_sections(
    boxes: List[Tuple[int, int, int, int]],
    all_text_regions: List[Tuple[str, Tuple[int, int, int, int]]],
    v_gap: int = 40,
) -> List[Tuple[int, int, int, int]]:
    """Flood-fill expand each box to cover the entire visual section it belongs to.

    Starting from a tight box around a detected new/deleted text token, this
    iteratively expands the box to absorb any text region that:
      - shares direct horizontal overlap with the current box (same column), AND
      - lies within *v_gap* pixels vertically (adjacent line or paragraph).

    Because every line in a section is within ~25 px of the next line, the fill
    naturally flows through all items in the section.  The fill stops when it
    reaches the inter-section whitespace (typically ≥ 40 px).

    This solves the problem where a section heading is detected as new but its
    list items exist elsewhere in the old page: the heading box expands to cover
    the full section regardless of whether individual items are "new" or not.
    """
    if not boxes:
        return boxes
    all_rects = [(nl, nt, nr, nb) for _, (nl, nt, nr, nb) in all_text_regions]
    result: List[Tuple[int, int, int, int]] = []
    for bl, bt, br, bb in boxes:
        changed = True
        while changed:
            changed = False
            for nl, nt, nr, nb in all_rects:
                if nr <= bl or nl >= br:          # no horizontal overlap
                    continue
                if nt > bb + v_gap or nb < bt - v_gap:   # too far vertically
                    continue
                new_box = (min(bl, nl), min(bt, nt), max(br, nr), max(bb, nb))
                if new_box != (bl, bt, br, bb):
                    bl, bt, br, bb = new_box
                    changed = True
        result.append((bl, bt, br, bb))
    return _merge_rect_list(result, gap=10)


def _filter_moved_boxes(
    boxes: List[Tuple[int, int, int, int]],
    new_text_regions: List[Tuple[str, Tuple[int, int, int, int]]],
    old_texts: Set[str],
) -> List[Tuple[int, int, int, int]]:
    """Remove diff boxes from the *new* image whose text content all exists in old.

    A box whose every detected text element is present somewhere in the old page is
    treated as "content that merely moved" rather than genuinely new content, and is
    therefore suppressed.  Boxes with no detectable text (image/colour changes) and
    boxes containing at least one brand-new text token are kept.
    """
    result: List[Tuple[int, int, int, int]] = []
    for bl, bt, br, bb in boxes:
        texts_in_box: Set[str] = set()
        for text, (tl, tt, tr, tb) in new_text_regions:
            if tr > bl and br > tl and tb > bt and bb > tt:
                texts_in_box.add(text)
        if not texts_in_box:
            result.append((bl, bt, br, bb))
        elif all(t in old_texts for t in texts_in_box):
            pass  # all content existed in old → moved, not new
        else:
            result.append((bl, bt, br, bb))
    return result


def _build_deleted_text_boxes(
    old_text_regions: List[Tuple[str, Tuple[int, int, int, int]]],
    new_text_regions: List[Tuple[str, Tuple[int, int, int, int]]],
    pad: int = 4,
) -> List[Tuple[int, int, int, int]]:
    """Return merged bounding boxes in the *old* image for text absent from the new page."""
    new_texts: Set[str] = {text for text, _ in new_text_regions}
    raw: List[Tuple[int, int, int, int]] = []
    for text, (tl, tt, tr, tb) in old_text_regions:
        if text not in new_texts:
            raw.append((max(0, tl - pad), max(0, tt - pad), tr + pad, tb + pad))
    return _merge_rect_list(raw)


def _save_image_diff(
    old_path: Path,
    new_path: Path,
    new_overlay_path: Path,
    old_overlay_path: Optional[Path] = None,
    old_text_regions: Optional[List[Tuple[str, Tuple[int, int, int, int]]]] = None,
    new_text_regions: Optional[List[Tuple[str, Tuple[int, int, int, int]]]] = None,
) -> Optional[Dict[str, object]]:
    """Generate annotated overlay images for the visual page diff.

    new_overlay_path – new screenshot with **orange** boxes around genuinely
        new or changed areas (content that only moved is suppressed).
    old_overlay_path – old screenshot with **red** boxes around text regions
        that were deleted (absent from the new page).  Only written when the
        parameter is supplied and text-region data is available.
    """
    if not PIL_AVAILABLE:
        return None

    # Crop trailing whitespace so oversized empty canvases don't dominate.
    old_img = _crop_trailing_whitespace(Image.open(old_path).convert("RGBA"))
    new_img = _crop_trailing_whitespace(Image.open(new_path).convert("RGBA"))

    old_w, old_h = old_img.size
    new_w, new_h = new_img.size
    common_w = min(old_w, new_w)
    common_h = min(old_h, new_h)

    # Pixel diff is kept only for summary METRICS (mean_delta, changed_ratio).
    # It is NOT used to determine visual annotation boxes, because page-level
    # layout shifts (sidebar additions, inserted sections) cause pixel differences
    # throughout even when content is identical, making pixel-diff boxes
    # misleading and overwhelming.
    compare_old_b = old_img.crop((0, 0, common_w, common_h)).filter(ImageFilter.GaussianBlur(radius=1))
    compare_new_b = new_img.crop((0, 0, common_w, common_h)).filter(ImageFilter.GaussianBlur(radius=1))
    diff = ImageChops.difference(compare_old_b, compare_new_b)
    stat = ImageStat.Stat(diff)
    total_pixels = common_w * common_h
    mean_delta = sum(stat.mean[:3]) / 3.0
    diff_luma = diff.convert("L")
    hist = diff_luma.histogram()
    non_zero = total_pixels - (hist[0] if hist else 0)
    changed_ratio = non_zero / max(1, total_pixels)

    old_regions_list: List[Tuple[str, Tuple[int, int, int, int]]] = old_text_regions or []
    new_regions_list: List[Tuple[str, Tuple[int, int, int, int]]] = new_text_regions or []

    # Text-content matching uses tokens of ≥ 3 characters.  Chinese characters
    # are each individually meaningful so 3 chars is a useful minimum; it also
    # filters out pure punctuation, bullet numbers ("1.", "▶"), etc.
    MIN_LEN = 3

    old_texts_set: Set[str] = {t for t, _ in old_regions_list if len(t) >= MIN_LEN}
    new_texts_set: Set[str] = {t for t, _ in new_regions_list if len(t) >= MIN_LEN}

    # ── New content boxes (new image) ────────────────────────────────────────
    # Primary method: TEXT-BASED DIFF.
    # A text token / block present in new but absent from old is genuinely new
    # content regardless of its position on the page.  Boxes are drawn around
    # those regions; everything that also exists in old is ignored.
    # Fallback: pixel diff, used only when Playwright text extraction returned
    # no data (e.g. the page was not loaded, JS execution failed, etc.).
    def _overlaps_any(box: Tuple[int, int, int, int], boxes: List[Tuple[int, int, int, int]]) -> bool:
        bl, bt, br, bb = box
        return any(fl < br and fr > bl and ft < bb and fb > bt for fl, ft, fr, fb in boxes)

    # Regions taller than this are considered "large" (multi-line blocks or
    # containers).  They are only used for boxing when no smaller region
    # already covers part of the same area, preventing a parent <li> or
    # <div> from over-highlighting when only one child item changed.
    _LARGE_H = 60

    if old_texts_set and new_texts_set:
        # Phase 1: small, fine-grained regions (individual text lines).
        new_raw_fine: List[Tuple[int, int, int, int]] = [
            (nl, nt, nr, nb)
            for text, (nl, nt, nr, nb) in new_regions_list
            if len(text) >= MIN_LEN
            and text not in old_texts_set
            and not text.startswith("\x01")
            and (nb - nt) <= _LARGE_H
        ]
        # Phase 2: large regions (tall block elements from Pass 2 AND Pass 3
        # containers) — include only when no fine-grained new entry already
        # covers part of the same area.
        for text, (nl, nt, nr, nb) in new_regions_list:
            if len(text) < MIN_LEN or text in old_texts_set:
                continue
            is_container = text.startswith("\x01")
            is_large_block = (not is_container) and (nb - nt) > _LARGE_H
            if not is_container and not is_large_block:
                continue
            if not _overlaps_any((nl, nt, nr, nb), new_raw_fine):
                new_raw_fine.append((nl, nt, nr, nb))
        new_boxes = _merge_rect_list(new_raw_fine, gap=15)
    else:
        # Pixel-diff fallback.
        mask = diff_luma.point(lambda x: 255 if x > 25 else 0)
        new_boxes = _build_diff_boxes(mask)

    # ── Deleted content boxes (old image) ────────────────────────────────────
    # Only small, fine-grained entries (Phase 1) are used for deleted boxes.
    # Large blocks / containers whose fingerprint merely changed (because
    # items were added or reordered) are NOT marked as deleted — the content
    # still exists in the new page, it was just modified.  If specific items
    # were truly removed, they appear as individual small entries.
    if old_texts_set and new_texts_set:
        deleted_boxes = _merge_rect_list(
            [
                (ol, ot, or_, ob)
                for text, (ol, ot, or_, ob) in old_regions_list
                if len(text) >= MIN_LEN
                and text not in new_texts_set
                and not text.startswith("\x01")
                and (ob - ot) <= _LARGE_H
            ],
            gap=15,
        )
    else:
        deleted_boxes = []

    # Regions that exist only in old or only in new due to page-height mismatch.
    old_extra_regions: List[Tuple[int, int, int, int]] = []
    new_extra_regions: List[Tuple[int, int, int, int]] = []
    if old_w > common_w:
        old_extra_regions.append((common_w, 0, old_w, old_h))
    if old_h > common_h:
        old_extra_regions.append((0, common_h, old_w, old_h))
    if new_w > common_w:
        new_extra_regions.append((common_w, 0, new_w, new_h))
    if new_h > common_h:
        new_extra_regions.append((0, common_h, new_w, new_h))

    canvas_w = max(old_w, new_w)
    canvas_h = max(old_h, new_h)

    def _draw_annotated(
        base: "Image.Image",
        boxes: List[Tuple[int, int, int, int]],
        fill_rgba: Tuple[int, int, int, int],
        outline_rgb: Tuple[int, int, int],
        extra_regions: List[Tuple[int, int, int, int]],
        extra_fill_rgba: Tuple[int, int, int, int],
        extra_outline_rgb: Tuple[int, int, int],
    ) -> "Image.Image":
        """Composite semi-transparent fills + opaque outlines onto *base*.

        PIL's ImageDraw does not alpha-blend fills; we draw on a separate
        transparent layer then alpha_composite so underlying text stays readable.
        """
        w, h = base.size
        fill_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        fd = ImageDraw.Draw(fill_layer)
        for l, t, r, b in boxes:
            fd.rectangle((l, t, r, b), fill=fill_rgba)
        for l, t, r, b in extra_regions:
            fd.rectangle((l, t, r, b), fill=extra_fill_rgba)
        result = Image.alpha_composite(base, fill_layer)
        od = ImageDraw.Draw(result)
        for l, t, r, b in boxes:
            od.rectangle((l, t, r, b), outline=outline_rgb + (255,), width=3)
        for l, t, r, b in extra_regions:
            od.rectangle((l, t, r, b), outline=extra_outline_rgb + (200,), width=2)
        return result

    # ── Draw new-image overlay (orange = new content) ────────────────────────
    new_base = Image.new("RGBA", (canvas_w, canvas_h), (255, 255, 255, 255))
    new_base.paste(new_img, (0, 0))
    new_overlay = _draw_annotated(
        new_base,
        boxes=new_boxes,
        fill_rgba=(249, 115, 22, 60),
        outline_rgb=(234, 88, 12),
        extra_regions=new_extra_regions,
        extra_fill_rgba=(249, 115, 22, 40),
        extra_outline_rgb=(234, 88, 12),
    )
    new_overlay_path.parent.mkdir(parents=True, exist_ok=True)
    new_overlay.save(new_overlay_path)

    # ── Draw old-image overlay (red = deleted content) ───────────────────────
    if old_overlay_path is not None:
        old_base = Image.new("RGBA", (canvas_w, canvas_h), (255, 255, 255, 255))
        old_base.paste(old_img, (0, 0))
        old_overlay_img = _draw_annotated(
            old_base,
            boxes=deleted_boxes,
            fill_rgba=(220, 38, 38, 60),
            outline_rgb=(220, 38, 38),
            extra_regions=old_extra_regions,
            extra_fill_rgba=(59, 130, 246, 40),
            extra_outline_rgb=(37, 99, 235),
        )
        old_overlay_path.parent.mkdir(parents=True, exist_ok=True)
        old_overlay_img.save(old_overlay_path)

    return {
        "same_size": old_img.size == new_img.size,
        "size_old": old_img.size,
        "size_new": new_img.size,
        "compare_region": (common_w, common_h),
        "mean_delta": round(mean_delta, 3),
        "changed_ratio": round(changed_ratio, 6),
        "box_count": len(new_boxes),
        "deleted_box_count": len(deleted_boxes),
        "old_extra_regions": old_extra_regions,
        "new_extra_regions": new_extra_regions,
        "text_based": bool(old_texts_set and new_texts_set),
        "note": (
            "Text-content diff: boxes show only genuinely new / deleted text tokens."
            if old_texts_set and new_texts_set
            else "Pixel diff fallback (text extraction unavailable)."
        ),
    }


def _safe_anchor(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _asset_src(path_value: str, cache_token: str) -> str:
    quoted_token = urllib.parse.quote(cache_token, safe="")
    safe_path = html.escape(path_value)
    sep = "&" if "?" in path_value else "?"
    return f"{safe_path}{sep}v={quoted_token}"


class ProgressBar:
    def __init__(self, total: int, enabled: bool = True, label: str = "Comparing") -> None:
        self.total = max(0, total)
        self.enabled = enabled and self.total > 0
        self.label = label
        self.current = 0
        self.last_print_at = 0.0
        self.width = 28

    def update(self, step: int = 1) -> None:
        if not self.enabled:
            return
        self.current = min(self.total, self.current + step)
        now = time.time()
        if (now - self.last_print_at) < 0.08 and self.current < self.total:
            return
        self.last_print_at = now
        self._render()

    def finish(self) -> None:
        if not self.enabled:
            return
        self.current = self.total
        self._render()
        print("", file=sys.stdout, flush=True)

    def _render(self) -> None:
        ratio = self.current / max(1, self.total)
        filled = int(self.width * ratio)
        bar = "#" * filled + "-" * (self.width - filled)
        percent = ratio * 100
        print(
            f"\r{self.label}: [{bar}] {self.current}/{self.total} ({percent:5.1f}%)",
            end="",
            file=sys.stdout,
            flush=True,
        )


class QuietHttpHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return

    def log_error(self, format: str, *args) -> None:
        return


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address) -> None:
        # Suppress noisy stack traces for client-aborted requests.
        return


class LocalSnapshotServer:
    def __init__(self, root: Path):
        self.root = root
        handler = partial(QuietHttpHandler, directory=str(root))
        self.httpd = QuietThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2.0)


_TEXT_REGION_JS = """() => {
    const out = [];
    const seen = new Set();
    const sx = window.scrollX || window.pageXOffset || 0;
    const sy = window.scrollY || window.pageYOffset || 0;

    function isHidden(el) {
        const s = window.getComputedStyle(el);
        return s.display === "none" || s.visibility === "hidden" || Number(s.opacity || "1") === 0;
    }

    function toDocRect(rect) {
        return {
            left: Math.round(rect.left + sx),
            top: Math.round(rect.top + sy),
            right: Math.round(rect.right + sx),
            bottom: Math.round(rect.bottom + sy),
        };
    }

    // Pass 1: text nodes (2–100 chars) — fine-grained tokens for matching.
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node = walker.nextNode();
    while (node) {
        const text = (node.textContent || "").replace(/\\s+/g, " ").trim();
        if (!text || text.length < 2 || text.length > 100) {
            node = walker.nextNode();
            continue;
        }
        const parent = node.parentElement;
        if (!parent || isHidden(parent)) {
            node = walker.nextNode();
            continue;
        }
        const range = document.createRange();
        range.selectNodeContents(node);
        const rect = range.getBoundingClientRect();
        if (!rect || rect.width < 10 || rect.height < 6) {
            node = walker.nextNode();
            continue;
        }
        const dr = toDocRect(rect);
        const key = text + "|" + dr.left + "|" + dr.top;
        if (!seen.has(key)) {
            seen.add(key);
            out.push({ text, ...dr });
        }
        node = walker.nextNode();
    }

    // Pass 2: block-level elements — use first 120 chars as fingerprint,
    // element bounding box covers the full rendered area for stable suppression.
    const BLOCK_TAGS = ["p","li","h1","h2","h3","h4","h5","h6","td","th","dt","dd","figcaption"];
    for (const tag of BLOCK_TAGS) {
        for (const el of document.querySelectorAll(tag)) {
            if (isHidden(el)) continue;
            const text = (el.textContent || "").replace(/\\s+/g, " ").trim();
            if (!text || text.length < 4) continue;
            const fingerprint = text.slice(0, 120);
            const rect = el.getBoundingClientRect();
            if (!rect || rect.width < 20 || rect.height < 8) continue;
            const dr = toDocRect(rect);
            const key = fingerprint + "|blk|" + dr.left + "|" + dr.top;
            if (!seen.has(key)) {
                seen.add(key);
                out.push({ text: fingerprint, ...dr });
            }
        }
    }

    // Pass 3: section-level container elements (div / section / article / aside).
    // These use large bounding boxes so they are ONLY used for text matching
    // (recognising moved vs genuinely new content), NOT for building highlight
    // boxes.  The \\x01 prefix lets Python distinguish them from Pass 1/2.
    //
    // Height range 20–800 px: lower bound 20 px captures single-line heading
    // widgets (Axure text rectangles are typically 25 px tall) while still
    // filtering sub-pixel wrappers; upper bound 800 px excludes page-level divs.
    for (const el of document.querySelectorAll("div, section, article, aside")) {
        if (isHidden(el)) continue;
        const rect = el.getBoundingClientRect();
        if (!rect || rect.height < 20 || rect.height > 800 || rect.width < 80) continue;
        const text = (el.textContent || "").replace(/\\s+/g, " ").trim();
        if (text.length < 10) continue;
        const fingerprint = text.slice(0, 400);
        const key = "cnt|" + fingerprint;
        if (seen.has(key)) continue;
        seen.add(key);
        const dr = toDocRect(rect);
        out.push({ text: "\\x01" + fingerprint, ...dr });
    }

    return out;
}"""


def _parse_text_regions(raw_regions: List[Dict[str, Any]]) -> List[Tuple[str, Tuple[int, int, int, int]]]:
    regions: List[Tuple[str, Tuple[int, int, int, int]]] = []
    for item in raw_regions:
        text = str(item.get("text", "")).strip()
        left = int(item.get("left", 0))
        top = int(item.get("top", 0))
        right = int(item.get("right", 0))
        bottom = int(item.get("bottom", 0))
        if not text or right <= left or bottom <= top:
            continue
        regions.append((text, (left, top, right, bottom)))
    return regions


async def _extract_text_regions_async(page) -> List[Tuple[str, Tuple[int, int, int, int]]]:
    try:
        raw_regions: List[Dict[str, Any]] = await page.evaluate(_TEXT_REGION_JS)
    except Exception:
        return []
    return _parse_text_regions(raw_regions)


async def _unlock_full_page(page) -> None:
    """Ensure Playwright captures the full page, including sidebar-heavy layouts.

    Two-pass approach:
    1. Inject CSS to remove overflow/height restrictions on <html> and <body> so
       the true scrollHeight becomes visible.
    2. Read the resulting scrollHeight and resize the viewport to that height.
       This causes elements sized with 100vh (e.g. fixed sidebars) to expand to
       the full content height, revealing all menu items.
    A final requestAnimationFrame waits for the layout to settle before the
    caller takes the screenshot.  The changes are in-memory only.
    """
    try:
        await page.add_style_tag(content=(
            "html, body {"
            "  overflow: visible !important;"
            "  height: auto !important;"
            "  max-height: none !important;"
            "}"
        ))
        await page.evaluate("() => new Promise(r => requestAnimationFrame(r))")

        # Measure the true content height after the overflow fix.
        dims = await page.evaluate("""() => ({
            w: window.innerWidth,
            h: Math.max(
                document.documentElement.scrollHeight,
                document.body ? document.body.scrollHeight : 0
            )
        })""")
        w = int(dims.get("w") or 0)
        h = int(dims.get("h") or 0)
        # Resize viewport so that 100vh / height:100% elements expand fully.
        if w > 0 and h > 0:
            await page.set_viewport_size({"width": w, "height": h})
            await page.evaluate("() => { window.scrollTo(0, 0); }")
            await page.evaluate("() => new Promise(r => requestAnimationFrame(r))")
    except Exception:
        pass


def _build_stable_text_regions(
    old_regions: Sequence[Tuple[str, Tuple[int, int, int, int]]],
    new_regions: Sequence[Tuple[str, Tuple[int, int, int, int]]],
) -> List[Tuple[int, int, int, int]]:
    old_by_text: Dict[str, List[Tuple[int, int, int, int]]] = {}
    new_by_text: Dict[str, List[Tuple[int, int, int, int]]] = {}
    for text, box in old_regions:
        old_by_text.setdefault(text, []).append(box)
    for text, box in new_regions:
        new_by_text.setdefault(text, []).append(box)

    stable: List[Tuple[int, int, int, int]] = []
    for text in set(old_by_text) & set(new_by_text):
        old_boxes = old_by_text[text]
        candidates = new_by_text[text]
        used: Set[int] = set()
        for old_box in old_boxes:
            ol, ot, or_, ob = old_box
            ow, oh = or_ - ol, ob - ot
            ocx = (ol + or_) / 2.0
            ocy = (ot + ob) / 2.0
            best_idx: Optional[int] = None
            best_score = float("inf")
            for idx, new_box in enumerate(candidates):
                if idx in used:
                    continue
                nl, nt, nr, nb = new_box
                nw, nh = nr - nl, nb - nt
                ncx = (nl + nr) / 2.0
                ncy = (nt + nb) / 2.0
                # Element size must be reasonably similar; large tolerance for
                # containers that reflow when neighbouring content changes.
                if abs(ow - nw) > 100 or abs(oh - nh) > 60:
                    continue
                # X-center tolerance covers sidebar-width changes that push the
                # main content column horizontally (up to ~100 px shift).
                if abs(ocx - ncx) > 100:
                    continue
                # Y may differ significantly when items are pushed down by insertions.
                # Use a balanced score so both axes matter equally.
                score = abs(ocx - ncx) * 2 + abs(ow - nw) + abs(oh - nh) + abs(ocy - ncy) * 0.05
                if score < best_score:
                    best_score = score
                    best_idx = idx
            if best_idx is None:
                continue
            used.add(best_idx)
            # Use new-image coordinates so suppression applies to the rendered new page.
            nl, nt, nr, nb = candidates[best_idx]
            # Small padding to cover anti-aliased edges of the element.
            pad = 4
            stable.append((max(0, nl - pad), max(0, nt - pad), nr + pad, nb + pad))

    stable = [box for box in stable if (box[2] - box[0]) >= 10 and (box[3] - box[1]) >= 6]
    return stable[:800]


async def _page_visual_diffs_async(
    old_root: Path,
    new_root: Path,
    output_dir: Path,
    candidates: List[str],
    new_only: List[str],
    viewport_width: int,
    viewport_height: int,
    wait_ms: int,
    progress: "ProgressBar",
    stats: Dict[str, int],
) -> Tuple[List[Dict[str, object]], Optional[str]]:
    items: List[Dict[str, object]] = []
    warning: Optional[str] = None

    old_server = LocalSnapshotServer(old_root)
    new_server = LocalSnapshotServer(new_root)
    old_server.start()
    new_server.start()

    try:
        async with async_playwright() as p:
            browser = None
            try:
                browser = await p.chromium.launch(headless=True)
            except PlaywrightError as exc:
                if "Executable doesn't exist" not in str(exc):
                    raise
                for channel in ("msedge", "chrome"):
                    try:
                        browser = await p.chromium.launch(headless=True, channel=channel)
                        break
                    except PlaywrightError:
                        continue
            if browser is None:
                warning = (
                    "Playwright browser is missing. Install with "
                    "`python -m playwright install chromium`, or ensure Edge/Chrome is installed."
                )
                return items, warning

            context = await browser.new_context(
                viewport={"width": max(200, viewport_width), "height": max(200, viewport_height)},
                ignore_https_errors=True,
            )
            # Two pages so old and new can load simultaneously.
            old_page = await context.new_page()
            new_page = await context.new_page()

            old_rel_ref: Dict[str, Optional[str]] = {"value": None}
            new_rel_ref: Dict[str, Optional[str]] = {"value": None}
            per_page_http_errors: Dict[str, Dict[str, object]] = {}

            async def _track_response(response, rel_ref: Dict[str, Optional[str]]) -> None:
                rel = rel_ref.get("value")
                if not rel:
                    return
                try:
                    status = int(response.status)
                except Exception:
                    return
                if status < 400:
                    return
                record = per_page_http_errors.setdefault(rel, {"count": 0, "samples": []})
                record["count"] = int(record.get("count", 0)) + 1
                samples = record.get("samples")
                if isinstance(samples, list) and len(samples) < 5:
                    samples.append(f"[{status}] {response.url}")

            old_page.on("response", lambda r: asyncio.ensure_future(_track_response(r, old_rel_ref)))
            new_page.on("response", lambda r: asyncio.ensure_future(_track_response(r, new_rel_ref)))

            loop = asyncio.get_event_loop()

            for rel in candidates:
                try:
                    old_rel_ref["value"] = rel
                    new_rel_ref["value"] = rel
                    quoted_rel = urllib.parse.quote(rel, safe="/")
                    old_url = f"http://127.0.0.1:{old_server.port}/{quoted_rel}"
                    new_url = f"http://127.0.0.1:{new_server.port}/{quoted_rel}"
                    anchor = _safe_anchor(f"page::{rel}")
                    old_preview = Path("assets") / f"{anchor}_page_old.png"
                    new_preview = Path("assets") / f"{anchor}_page_new.png"
                    overlay_preview = Path("assets") / f"{anchor}_page_overlay.png"
                    old_overlay_preview = Path("assets") / f"{anchor}_page_old_overlay.png"

                    # Navigate both pages in parallel.
                    await asyncio.gather(
                        old_page.goto(old_url, wait_until="load", timeout=60000),
                        new_page.goto(new_url, wait_until="load", timeout=60000),
                    )
                    if wait_ms > 0:
                        await asyncio.gather(
                            old_page.wait_for_timeout(wait_ms),
                            new_page.wait_for_timeout(wait_ms),
                        )

                    # Unlock overflow and resize viewport to content height FIRST so
                    # that 100vh sidebars expand fully.  Text region coordinates are
                    # then extracted from the already-expanded layout, keeping them
                    # consistent with what the screenshot will capture.
                    await asyncio.gather(
                        _unlock_full_page(old_page),
                        _unlock_full_page(new_page),
                    )

                    # Extract text regions from both pages in parallel.
                    old_text_regions, new_text_regions = await asyncio.gather(
                        _extract_text_regions_async(old_page),
                        _extract_text_regions_async(new_page),
                    )

                    # Screenshot both pages in parallel.
                    await asyncio.gather(
                        old_page.screenshot(path=str(output_dir / old_preview), full_page=True),
                        new_page.screenshot(path=str(output_dir / new_preview), full_page=True),
                    )

                    stats["compared"] += 1
                    old_hash, new_hash = await asyncio.gather(
                        loop.run_in_executor(None, sha1_file, output_dir / old_preview),
                        loop.run_in_executor(None, sha1_file, output_dir / new_preview),
                    )

                    error_info = per_page_http_errors.get(rel, {"count": 0, "samples": []})
                    error_count = int(error_info.get("count", 0))
                    error_samples = (
                        error_info.get("samples", [])
                        if isinstance(error_info.get("samples", []), list)
                        else []
                    )
                    if error_count > 0:
                        stats["http_error_pages"] += 1
                        stats["http_error_requests"] += error_count

                    if old_hash == new_hash:
                        stats["skipped"] += 1
                        continue

                    metrics = await loop.run_in_executor(
                        None,
                        _save_image_diff,
                        output_dir / old_preview,
                        output_dir / new_preview,
                        output_dir / overlay_preview,
                        output_dir / old_overlay_preview,
                        old_text_regions,
                        new_text_regions,
                    )

                    # Skip pages where the image diff engine finds no visible changes.
                    # Screenshot hashes can differ due to sub-pixel rendering noise even
                    # when the page looks identical; only surface items with real boxes.
                    # Pages whose length changed (new_extra_regions / old_extra_regions)
                    # are always shown even when the overlapping region looks identical.
                    if metrics is not None:
                        has_boxes = (
                            int(metrics.get("box_count", 0)) > 0
                            or int(metrics.get("deleted_box_count", 0)) > 0
                        )
                        has_size_change = bool(
                            metrics.get("new_extra_regions") or metrics.get("old_extra_regions")
                        )
                        if not has_boxes and not has_size_change:
                            stats["skipped"] += 1
                            continue

                    stats["changed"] += 1
                    items.append(
                        {
                            "path": rel,
                            "old_hash": old_hash[:10],
                            "new_hash": new_hash[:10],
                            "old_preview": old_preview.as_posix(),
                            "new_preview": new_preview.as_posix(),
                            "overlay_preview": (
                                overlay_preview.as_posix()
                                if metrics and (output_dir / overlay_preview).exists()
                                else ""
                            ),
                            "old_overlay_preview": (
                                old_overlay_preview.as_posix()
                                if metrics and (output_dir / old_overlay_preview).exists()
                                else ""
                            ),
                            "metrics": metrics or {"note": "Pillow not installed; only screenshot compared."},
                            "http_error_count": error_count,
                            "http_error_samples": error_samples,
                            "is_new_page": False,
                        }
                    )
                except Exception:
                    stats["skipped"] += 1
                finally:
                    old_rel_ref["value"] = None
                    new_rel_ref["value"] = None
                    progress.update()

            # Render new-only pages (no old version to compare against).
            for rel in new_only:
                try:
                    new_rel_ref["value"] = rel
                    quoted_rel = urllib.parse.quote(rel, safe="/")
                    new_url = f"http://127.0.0.1:{new_server.port}/{quoted_rel}"
                    anchor = _safe_anchor(f"page::{rel}")
                    new_preview = Path("assets") / f"{anchor}_page_new.png"

                    await new_page.goto(new_url, wait_until="load", timeout=60000)
                    if wait_ms > 0:
                        await new_page.wait_for_timeout(wait_ms)
                    await _unlock_full_page(new_page)
                    await new_page.screenshot(path=str(output_dir / new_preview), full_page=True)

                    new_hash = await loop.run_in_executor(None, sha1_file, output_dir / new_preview)
                    stats["changed"] += 1
                    items.append(
                        {
                            "path": rel,
                            "old_hash": "",
                            "new_hash": new_hash[:10],
                            "old_preview": "",
                            "new_preview": new_preview.as_posix(),
                            "overlay_preview": "",
                            "metrics": {"note": "新增頁面，無舊版可比較。"},
                            "http_error_count": 0,
                            "http_error_samples": [],
                            "is_new_page": True,
                        }
                    )
                except Exception:
                    stats["skipped"] += 1
                finally:
                    new_rel_ref["value"] = None
                    progress.update()

            await browser.close()
    except PlaywrightError as exc:
        warning = f"Playwright failed; skipped page visual diff. {exc}"
    finally:
        old_server.stop()
        new_server.stop()

    return items, warning


def generate_page_visual_diffs(
    old_root: Path,
    new_root: Path,
    output_dir: Path,
    max_pages: int,
    viewport_width: int,
    viewport_height: int,
    wait_ms: int,
    show_progress: bool,
) -> Tuple[List[Dict[str, object]], Dict[str, int], Optional[str]]:
    if not PLAYWRIGHT_AVAILABLE:
        return [], {
            "total_candidates": 0,
            "compared": 0,
            "changed": 0,
            "skipped": 0,
            "http_error_pages": 0,
            "http_error_requests": 0,
        }, "Playwright not installed; skipped page visual diff."

    old_files = collect_files(old_root)
    new_files = collect_files(new_root)
    candidates = [
        rel
        for rel in sorted(set(old_files) & set(new_files))
        if Path(rel).suffix.lower() in {".html", ".htm"}
    ]
    new_only = [
        rel
        for rel in sorted(set(new_files) - set(old_files))
        if Path(rel).suffix.lower() in {".html", ".htm"}
    ]
    if max_pages > 0:
        candidates = candidates[:max_pages]
        remaining = max(0, max_pages - len(candidates))
        new_only = new_only[:remaining]

    total_count = len(candidates) + len(new_only)
    if total_count == 0:
        return [], {
            "total_candidates": 0,
            "compared": 0,
            "changed": 0,
            "skipped": 0,
            "http_error_pages": 0,
            "http_error_requests": 0,
        }, None

    stats: Dict[str, int] = {
        "total_candidates": total_count,
        "compared": 0,
        "changed": 0,
        "skipped": 0,
        "http_error_pages": 0,
        "http_error_requests": 0,
    }
    progress = ProgressBar(total=total_count, enabled=show_progress, label="Rendering pages")
    items: List[Dict[str, object]] = []
    warning: Optional[str] = None

    try:
        items, warning = asyncio.run(
            _page_visual_diffs_async(
                old_root=old_root,
                new_root=new_root,
                output_dir=output_dir,
                candidates=candidates,
                new_only=new_only,
                viewport_width=viewport_width,
                viewport_height=viewport_height,
                wait_ms=wait_ms,
                progress=progress,
                stats=stats,
            )
        )
    except Exception as exc:
        warning = f"Playwright failed; skipped page visual diff. {exc}"
    finally:
        progress.finish()

    return items, stats, warning


def generate_report(
    old_root: Path,
    new_root: Path,
    output_dir: Path,
    text_context_lines: int,
    max_text_diff_lines: int,
    show_progress: bool,
    page_visual_enabled: bool,
    page_visual_max_pages: int,
    page_visual_width: int,
    page_visual_height: int,
    page_visual_wait_ms: int,
    quick_mode: bool,
) -> Path:
    old_files = collect_files(old_root)
    new_files = collect_files(new_root)
    all_paths = sorted(set(old_files) | set(new_files))

    report_assets = output_dir / "assets"
    output_dir.mkdir(parents=True, exist_ok=True)
    report_assets.mkdir(parents=True, exist_ok=True)

    summary = {
        "added": 0,
        "removed": 0,
        "modified_text": 0,
        "modified_image": 0,
        "skipped_text": 0,
        "skipped_image": 0,
        "modified_page_visual": 0,
        "modified_other": 0,
        "unchanged": 0,
    }
    text_items: List[Dict[str, object]] = []
    image_items: List[Dict[str, object]] = []
    other_items: List[Dict[str, object]] = []
    added_items: List[str] = []
    removed_items: List[str] = []
    if quick_mode:
        common_paths = sorted(set(old_files) & set(new_files))
        for rel in common_paths:
            old_path = old_files[rel]
            new_path = new_files[rel]
            old_ext = old_path.suffix.lower()
            new_ext = new_path.suffix.lower()
            if old_ext in TEXT_EXTENSIONS and new_ext in TEXT_EXTENSIONS:
                summary["skipped_text"] += 1
            elif old_ext in IMAGE_EXTENSIONS and new_ext in IMAGE_EXTENSIONS:
                summary["skipped_image"] += 1
    else:
        progress = ProgressBar(total=len(all_paths), enabled=show_progress, label="Comparing files")
        for rel in all_paths:
            try:
                old_path = old_files.get(rel)
                new_path = new_files.get(rel)

                if old_path is None and new_path is not None:
                    summary["added"] += 1
                    added_items.append(rel)
                    continue
                if new_path is None and old_path is not None:
                    summary["removed"] += 1
                    removed_items.append(rel)
                    continue
                if old_path is None or new_path is None:
                    continue

                old_hash = sha1_file(old_path)
                new_hash = sha1_file(new_path)
                if old_hash == new_hash:
                    summary["unchanged"] += 1
                    continue

                if is_text_file(old_path) and is_text_file(new_path):
                    summary["modified_text"] += 1
                    side_by_side = build_side_by_side_rows(
                        old_file=old_path,
                        new_file=new_path,
                        context_lines=text_context_lines,
                        max_rows=max_text_diff_lines,
                    )
                    text_items.append(
                        {
                            "path": rel,
                            "old_hash": old_hash[:10],
                            "new_hash": new_hash[:10],
                            "rows": side_by_side["rows"],
                            "truncated": side_by_side["truncated"],
                            "change_stats": side_by_side["change_stats"],
                        }
                    )
                    continue

                if is_image_file(old_path) and is_image_file(new_path):
                    summary["modified_image"] += 1
                    anchor = _safe_anchor(rel)
                    old_preview = Path("assets") / f"{anchor}_old{old_path.suffix.lower() or '.bin'}"
                    new_preview = Path("assets") / f"{anchor}_new{new_path.suffix.lower() or '.bin'}"
                    overlay_preview = Path("assets") / f"{anchor}_overlay.png"

                    _save_image_preview(old_path, output_dir / old_preview)
                    _save_image_preview(new_path, output_dir / new_preview)
                    metrics = _save_image_diff(old_path, new_path, output_dir / overlay_preview)

                    image_items.append(
                        {
                            "path": rel,
                            "old_hash": old_hash[:10],
                            "new_hash": new_hash[:10],
                            "old_preview": old_preview.as_posix(),
                            "new_preview": new_preview.as_posix(),
                            "overlay_preview": (
                                overlay_preview.as_posix()
                                if metrics
                                and (output_dir / overlay_preview).exists()
                                else ""
                            ),
                            "metrics": metrics or {"note": "Pillow not installed; only hash/preview compared."},
                        }
                    )
                    continue

                summary["modified_other"] += 1
                other_items.append(
                    {
                        "path": rel,
                        "old_hash": old_hash[:10],
                        "new_hash": new_hash[:10],
                    }
                )
            finally:
                progress.update()
        progress.finish()

    page_visual_items: List[Dict[str, object]] = []
    page_visual_stats = {
        "total_candidates": 0,
        "compared": 0,
        "changed": 0,
        "skipped": 0,
        "http_error_pages": 0,
        "http_error_requests": 0,
    }
    page_visual_warning: Optional[str] = None
    if page_visual_enabled:
        page_visual_items, page_visual_stats, page_visual_warning = generate_page_visual_diffs(
            old_root=old_root,
            new_root=new_root,
            output_dir=output_dir,
            max_pages=page_visual_max_pages,
            viewport_width=page_visual_width,
            viewport_height=page_visual_height,
            wait_ms=page_visual_wait_ms,
            show_progress=show_progress,
        )
        summary["modified_page_visual"] = len(page_visual_items)

    generated_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload = {
        "generated_at": generated_at,
        "old_root": old_root.as_posix(),
        "new_root": new_root.as_posix(),
        "summary": summary,
        "text_items": text_items,
        "image_items": image_items,
        "other_items": other_items,
        "added_items": added_items,
        "removed_items": removed_items,
        "page_visual_items": page_visual_items,
        "page_visual_stats": page_visual_stats,
        "page_visual_warning": page_visual_warning or "",
        "quick_mode": quick_mode,
        "cache_token": str(int(time.time() * 1000)),
        "pil_available": PIL_AVAILABLE,
        "playwright_available": PLAYWRIGHT_AVAILABLE,
    }
    (output_dir / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    html_report = render_html(payload)
    report_file = output_dir / "report.html"
    report_file.write_text(html_report, encoding="utf-8")
    return report_file


def _render_summary_card(summary: Dict[str, int], quick_mode: bool) -> str:
    parts = []
    card_defs = []
    if not quick_mode:
        card_defs.extend(
            [
                ("added", "新增", "section-added"),
                ("removed", "刪除", "section-removed"),
            ]
        )
    if quick_mode:
        card_defs.extend(
            [
                ("skipped_text", "略過文字", "section-skip"),
                ("skipped_image", "略過圖片", "section-skip"),
            ]
        )
    else:
        card_defs.extend(
            [
                ("modified_text", "文字異動", "section-text"),
                ("modified_image", "圖片異動", "section-image"),
            ]
        )
    card_defs.extend(
        [("modified_page_visual", "頁面視覺異動", "section-page-visual")]
    )
    if not quick_mode:
        card_defs.append(("modified_other", "其他異動", "section-other"))
    if not quick_mode:
        card_defs.append(("unchanged", "未變更", "section-unchanged"))

    for key, label, anchor in card_defs:
        parts.append(
            "<a class='metric metric-link' href='#"
            + anchor
            + "'>"
            + f"<div class='metric-value'>{summary.get(key, 0)}</div>"
            + f"<div class='metric-label'>{label}</div>"
            + "</a>"
        )
    return "".join(parts)


def _render_text_items(items: Sequence[Dict[str, object]]) -> str:
    if not items:
        return "<p class='empty'>沒有偵測到文字異動。</p>"
    chunks: List[str] = []
    for item in items:
        truncated_note = "（已截斷）" if item.get("truncated") else ""
        stats = item.get("change_stats", {})
        add_n = int(stats.get("insert", 0)) if isinstance(stats, dict) else 0
        del_n = int(stats.get("delete", 0)) if isinstance(stats, dict) else 0
        rep_n = int(stats.get("replace", 0)) if isinstance(stats, dict) else 0
        rows = item.get("rows", [])
        table_rows: List[str] = []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                tag = str(row.get("tag", "equal"))
                old_no = "" if row.get("old_no") is None else str(row.get("old_no"))
                new_no = "" if row.get("new_no") is None else str(row.get("new_no"))
                old_text_raw = str(row.get("old_text", ""))
                new_text_raw = str(row.get("new_text", ""))
                if tag == "replace":
                    old_html, new_html = _highlight_line_diff(old_text_raw, new_text_raw)
                else:
                    old_html = html.escape(old_text_raw)
                    new_html = html.escape(new_text_raw)

                if tag == "insert":
                    new_html = f"<span class='hl-ins'>{new_html}</span>"
                elif tag == "delete":
                    old_html = f"<span class='hl-del'>{old_html}</span>"
                elif tag == "skip":
                    old_html = f"<span class='muted'>{old_html}</span>"
                    new_html = f"<span class='muted'>{new_html}</span>"

                table_rows.append(
                    "<tr class='row-" + tag + "'>"
                    f"<td class='ln'>{old_no}</td><td><code>{old_html}</code></td>"
                    f"<td class='ln'>{new_no}</td><td><code>{new_html}</code></td>"
                    "</tr>"
                )
        chunks.append(
            "<details class='card'>"
            f"<summary><code>{html.escape(str(item['path']))}</code> "
            f"<span class='hash'>[{item['old_hash']} -> {item['new_hash']}] {truncated_note}</span> "
            f"<span class='chip chip-ins'>+{add_n}</span> "
            f"<span class='chip chip-del'>-{del_n}</span> "
            f"<span class='chip chip-rep'>~{rep_n}</span>"
            "</summary>"
            "<div class='text-diff-wrap'><table class='text-diff'>"
            "<thead><tr><th colspan='2'>舊版</th><th colspan='2'>新版</th></tr></thead>"
            "<tbody>"
            + "".join(table_rows)
            + "</tbody></table></div>"
            "</details>"
        )
    return "\n".join(chunks)


def _render_image_items(items: Sequence[Dict[str, object]], cache_token: str) -> str:
    if not items:
        return "<p class='empty'>沒有偵測到圖片異動。</p>"
    chunks: List[str] = []
    for item in items:
        item_path = str(item["path"])
        item_path_escaped = html.escape(item_path)
        metrics_json = html.escape(
            json.dumps(item.get("metrics", {}), ensure_ascii=False, indent=2)
        )
        error_samples = item.get("http_error_samples", []) or []
        error_block = (
            "<div><h4>失敗請求樣本</h4><ul>"
            + "".join(f"<li><code>{html.escape(str(u))}</code></li>" for u in error_samples)
            + "</ul></div>"
            if error_samples
            else ""
        )
        overlay = item.get("overlay_preview") or ""
        overlay_block = (
            "<div class='img-panel'><h4>差異框選（紅框：共同區域差異；橘區：僅新版；藍區：僅舊版）</h4>"
            f"<img class='preview-img js-zoomable' src='{_asset_src(str(overlay), cache_token)}' "
            f"alt='overlay' data-caption='{html.escape(f'{item_path} - 差異遮罩', quote=True)}'></div>"
            if overlay
            else "<div><h4>差異高亮</h4><p class='empty'>未產生（可能為尺寸不同或未安裝 Pillow）</p></div>"
        )
        chunks.append(
            "<details class='card'>"
            f"<summary><code>{item_path_escaped}</code> "
            f"<span class='hash'>[{item['old_hash']} -> {item['new_hash']}]</span></summary>"
            "<div class='img-grid'>"
            "<div class='img-panel'><h4>舊版</h4>"
            f"<img class='preview-img js-zoomable' src='{_asset_src(str(item['old_preview']), cache_token)}' "
            f"alt='old' data-caption='{html.escape(f'{item_path} - 舊版', quote=True)}'></div>"
            "<div class='img-panel'><h4>新版</h4>"
            f"<img class='preview-img js-zoomable' src='{_asset_src(str(item['new_preview']), cache_token)}' "
            f"alt='new' data-caption='{html.escape(f'{item_path} - 新版', quote=True)}'></div>"
            f"{overlay_block}"
            "</div>"
            f"<pre>{metrics_json}</pre>"
            "</details>"
        )
    return "\n".join(chunks)


def _render_page_visual_items(
    items: Sequence[Dict[str, object]],
    stats: Dict[str, object],
    warning: str,
    cache_token: str,
) -> str:
    stat_text = (
        f"<p class='meta-inline'>候選頁面：{int(stats.get('total_candidates', 0))}，"
        f"已比較：{int(stats.get('compared', 0))}，"
        f"有差異：{int(stats.get('changed', 0))}，"
        f"跳過/無差異：{int(stats.get('skipped', 0))}，"
        f"資源載入失敗頁數：{int(stats.get('http_error_pages', 0))}，"
        f"失敗請求數：{int(stats.get('http_error_requests', 0))}</p>"
    )
    if warning:
        stat_text += f"<p class='warn'>{html.escape(warning)}</p>"

    if not items:
        return stat_text + "<p class='empty'>沒有偵測到頁面視覺異動。</p>"

    chunks: List[str] = [stat_text]
    for item in items:
        item_path = str(item["path"])
        item_path_escaped = html.escape(item_path)
        is_new_page = bool(item.get("is_new_page", False))
        metrics_json = html.escape(
            json.dumps(item.get("metrics", {}), ensure_ascii=False, indent=2)
        )
        error_samples = item.get("http_error_samples", []) or []
        error_block = (
            "<div><h4>失敗請求樣本</h4><ul>"
            + "".join(f"<li><code>{html.escape(str(u))}</code></li>" for u in error_samples)
            + "</ul></div>"
            if error_samples
            else ""
        )
        overlay = item.get("overlay_preview") or ""
        old_overlay = item.get("old_overlay_preview") or ""

        if is_new_page:
            # New-only page: show only new version screenshot, no comparison.
            new_img_src = _asset_src(str(item.get("new_preview", "")), cache_token)
            summary_badge = "<span class='chip chip-ins'>新增頁面</span>"
            chunks.append(
                "<details class='card'>"
                f"<summary><code>{item_path_escaped}</code> "
                f"{summary_badge}</summary>"
                "<div class='img-grid page-grid'>"
                "<div class='img-panel'><h4>新版畫面（僅新版存在）</h4>"
                f"<img class='preview-img js-zoomable' src='{new_img_src}' "
                f"alt='new-page' data-caption='{html.escape(f'{item_path} - 新版畫面', quote=True)}'></div>"
                "</div>"
                f"<pre>{metrics_json}</pre>"
                "</details>"
            )
        else:
            fail_chip = (
                f" <span class='chip chip-del'>fail {int(item.get('http_error_count', 0))}</span>"
                if int(item.get("http_error_count", 0)) > 0
                else ""
            )
            # Choose annotated images when available, fall back to raw screenshots.
            old_src = _asset_src(old_overlay or str(item["old_preview"]), cache_token)
            old_label = "舊版畫面（紅框：已刪除內容）" if old_overlay else "舊版畫面"
            old_caption = html.escape(f'{item_path} - 舊版畫面', quote=True)

            new_src = _asset_src(overlay or str(item["new_preview"]), cache_token)
            new_label = "新版畫面（橘框：新增／異動內容）" if overlay else "新版畫面"
            new_caption = html.escape(f'{item_path} - 新版畫面', quote=True)

            chunks.append(
                "<details class='card'>"
                f"<summary><code>{item_path_escaped}</code> "
                f"<span class='hash'>[{item['old_hash']} -> {item['new_hash']}]</span>"
                f"{fail_chip}</summary>"
                "<div class='img-grid page-grid'>"
                f"<div class='img-panel'><h4>{old_label}</h4>"
                f"<img class='preview-img js-zoomable' src='{old_src}' "
                f"alt='old-page' data-caption='{old_caption}'></div>"
                f"<div class='img-panel'><h4>{new_label}</h4>"
                f"<img class='preview-img js-zoomable' src='{new_src}' "
                f"alt='new-page' data-caption='{new_caption}'></div>"
                "</div>"
                f"{error_block}"
                f"<pre>{metrics_json}</pre>"
                "</details>"
            )
    return "\n".join(chunks)


def _render_simple_list(items: Sequence[str], empty_text: str) -> str:
    if not items:
        return f"<p class='empty'>{html.escape(empty_text)}</p>"
    li = "\n".join(f"<li><code>{html.escape(p)}</code></li>" for p in items)
    return f"<ul>{li}</ul>"


def _render_other_items(items: Sequence[Dict[str, object]]) -> str:
    if not items:
        return "<p class='empty'>沒有其他二進位檔異動。</p>"
    li = "\n".join(
        (
            f"<li><code>{html.escape(str(i['path']))}</code> "
            f"<span class='hash'>[{i['old_hash']} -> {i['new_hash']}]</span></li>"
        )
        for i in items
    )
    return f"<ul>{li}</ul>"


def _render_dependency_banner(pil_ok: bool, playwright_ok: bool) -> str:
    if pil_ok and playwright_ok:
        return ""
    items: List[str] = []
    if not pil_ok:
        items.append(
            "<li>"
            "<strong>Pillow</strong>（圖片差異框選 / 像素比對）未安裝。"
            " 安裝指令：<code>pip install pillow</code>"
            "</li>"
        )
    if not playwright_ok:
        items.append(
            "<li>"
            "<strong>Playwright</strong>（頁面截圖視覺比對）未安裝。"
            " 安裝指令：<code>pip install playwright</code>"
            "，接著執行 <code>python -m playwright install chromium</code>"
            "</li>"
        )
    return (
        "<div class='dep-banner'>"
        "<strong>⚠ 缺少相依套件，以下功能已停用：</strong>"
        f"<ul>{''.join(items)}</ul>"
        "</div>"
    )


def render_html(payload: Dict[str, object]) -> str:
    quick_mode = bool(payload.get("quick_mode", False))
    cache_token = str(payload.get("cache_token", "0"))
    dep_banner_html = _render_dependency_banner(
        pil_ok=bool(payload.get("pil_available", True)),
        playwright_ok=bool(payload.get("playwright_available", True)),
    )
    summary_html = _render_summary_card(payload["summary"], quick_mode=quick_mode)  # type: ignore[index]
    text_html = _render_text_items(payload["text_items"]) if not quick_mode else ""
    image_html = _render_image_items(payload["image_items"], cache_token=cache_token) if not quick_mode else ""
    page_visual_html = _render_page_visual_items(
        payload.get("page_visual_items", []),  # type: ignore[arg-type]
        payload.get("page_visual_stats", {}),  # type: ignore[arg-type]
        str(payload.get("page_visual_warning", "")),
        cache_token=cache_token,
    )
    other_html = _render_other_items(payload["other_items"])  # type: ignore[index]
    added_html = _render_simple_list(payload["added_items"], "沒有新增檔案。")  # type: ignore[index]
    removed_html = _render_simple_list(payload["removed_items"], "沒有刪除檔案。")  # type: ignore[index]
    skip_section_html = (
        (
            '<section class="section anchor-offset" id="section-skip">'
            "<h2>快速模式略過項目</h2>"
            "<p>目前啟用快速模式，已略過以下詳細比對，以加快整體速度：</p>"
            "<ul>"
            f"<li>文字異動：<strong>{payload['summary'].get('skipped_text', 0)}</strong> 個檔案</li>"
            f"<li>圖片異動：<strong>{payload['summary'].get('skipped_image', 0)}</strong> 個檔案</li>"
            "</ul>"
            "<p class='hint'>若要完整報告，請改用 <code>--full-report</code>。</p>"
            "</section>"
        )
        if quick_mode
        else ""
    )
    text_section_html = (
        (
            '<section class="section anchor-offset" id="section-text">'
            "<h2>文字異動</h2>"
            f"{text_html}"
            "</section>"
        )
        if not quick_mode
        else ""
    )
    image_section_html = (
        (
            '<section class="section anchor-offset" id="section-image">'
            "<h2>圖片異動</h2>"
            '<p class="hint">可點擊任一圖片放大檢視，按 Esc 或點背景關閉。</p>'
            f"{image_html}"
            "</section>"
        )
        if not quick_mode
        else ""
    )
    other_section_html = (
        (
            '<section class="section anchor-offset" id="section-other">'
            "<h2>其他二進位檔異動</h2>"
            f"{other_html}"
            "</section>"
        )
        if not quick_mode
        else ""
    )
    added_section_html = (
        (
            '<section class="section anchor-offset" id="section-added">'
            "<h2>新增檔案</h2>"
            f"{added_html}"
            "</section>"
        )
        if not quick_mode
        else ""
    )
    removed_section_html = (
        (
            '<section class="section anchor-offset" id="section-removed">'
            "<h2>刪除檔案</h2>"
            f"{removed_html}"
            "</section>"
        )
        if not quick_mode
        else ""
    )
    unchanged_section_html = (
        (
            '<section class="section anchor-offset" id="section-unchanged">'
            "<h2>未變更統計</h2>"
            f"<p>未變更檔案數：<strong>{payload['summary'].get('unchanged', 0)}</strong></p>"
            "</section>"
        )
        if not quick_mode
        else ""
    )

    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>網站版本差異報告</title>
  <style>
    body {{
      font-family: "Segoe UI", "Noto Sans TC", sans-serif;
      margin: 24px;
      line-height: 1.5;
      background: #f7f8fb;
      color: #1f2330;
    }}
    h1, h2 {{ margin: 0.4em 0; }}
    .meta {{ color: #5b6070; margin-bottom: 16px; }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
      gap: 10px;
      margin-bottom: 20px;
    }}
    .metric {{
      background: white;
      border: 1px solid #dce1ef;
      border-radius: 10px;
      padding: 10px;
      text-align: center;
    }}
    .metric-link {{
      text-decoration: none;
      color: inherit;
      display: block;
      transition: all 120ms ease;
    }}
    .metric-link:hover {{
      border-color: #b6c6f3;
      transform: translateY(-1px);
      box-shadow: 0 4px 14px rgba(41, 72, 177, 0.08);
    }}
    .metric-value {{
      font-size: 1.5rem;
      font-weight: 700;
      color: #2948b1;
    }}
    .metric-label {{ color: #5b6070; font-size: 0.9rem; }}
    .meta-inline {{ color: #5b6070; margin-top: 0; }}
    .warn {{
      background: #fff5d8;
      color: #6f4d00;
      border: 1px solid #f0de9d;
      border-radius: 6px;
      padding: 8px 10px;
      margin: 10px 0;
    }}
    .dep-banner {{
      background: #fff3cd;
      color: #5a3e00;
      border: 1px solid #f5c842;
      border-left: 5px solid #f0a500;
      border-radius: 8px;
      padding: 12px 16px;
      margin-bottom: 18px;
      font-size: 14px;
    }}
    .dep-banner ul {{
      margin: 6px 0 0 0;
      padding-left: 20px;
    }}
    .dep-banner li {{
      margin: 4px 0;
    }}
    .dep-banner code {{
      background: #ffeaa0;
      border-radius: 3px;
      padding: 1px 5px;
    }}
    .section {{
      margin: 18px 0;
      background: white;
      border: 1px solid #dce1ef;
      border-radius: 10px;
      padding: 14px;
    }}
    details.card {{
      border: 1px solid #e4e8f3;
      border-radius: 8px;
      margin: 10px 0;
      padding: 8px;
      background: #fbfcff;
    }}
    details.card summary {{
      cursor: pointer;
      font-weight: 600;
    }}
    pre {{
      overflow: auto;
      background: #121621;
      color: #e8ebf7;
      padding: 10px;
      border-radius: 6px;
      font-size: 12px;
    }}
    code {{
      font-family: "Consolas", "Courier New", monospace;
      font-size: 12px;
    }}
    .hash {{ color: #6f7690; font-size: 12px; }}
    .chip {{
      display: inline-block;
      border-radius: 999px;
      padding: 0 8px;
      font-size: 11px;
      line-height: 18px;
      margin-left: 4px;
      border: 1px solid transparent;
    }}
    .chip-ins {{ color: #0a6e2f; background: #e8f7ee; border-color: #bde8cc; }}
    .chip-del {{ color: #a61b1b; background: #fdeeee; border-color: #f3c8c8; }}
    .chip-rep {{ color: #6e4f0a; background: #fff8e7; border-color: #f2dfab; }}
    .text-diff-wrap {{
      overflow: auto;
      border: 1px solid #dce1ef;
      border-radius: 8px;
      margin-top: 8px;
    }}
    table.text-diff {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      font-size: 12px;
    }}
    .text-diff th {{
      position: sticky;
      top: 0;
      z-index: 1;
      background: #f1f4fc;
      border-bottom: 1px solid #dce1ef;
      padding: 6px;
      text-align: left;
    }}
    .text-diff td {{
      border-top: 1px solid #edf0f8;
      vertical-align: top;
      padding: 4px 6px;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    .text-diff td.ln {{
      width: 48px;
      color: #7a8094;
      text-align: right;
      background: #fafbff;
    }}
    .row-insert td:nth-child(3), .row-insert td:nth-child(4) {{ background: #ecfaef; }}
    .row-delete td:nth-child(1), .row-delete td:nth-child(2) {{ background: #fff1f1; }}
    .row-replace td {{ background: #fff9e8; }}
    .row-skip td {{ background: #f8f9fd; }}
    .hl-ins {{ background: #c8f1d6; border-radius: 2px; }}
    .hl-del {{ background: #f8cccc; border-radius: 2px; }}
    .muted {{ color: #8c92a8; font-style: italic; }}
    .img-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
      gap: 12px;
      margin-top: 8px;
    }}
    .img-panel h4 {{
      margin: 0 0 6px;
      color: #4f5670;
      font-size: 13px;
    }}
    .preview-img {{
      width: 100%;
      height: auto;
      display: block;
      cursor: zoom-in;
    }}
    .img-grid img {{
      border: 1px solid #dce1ef;
      border-radius: 6px;
      background: white;
    }}
    .page-grid {{
      grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
    }}
    .page-grid img {{
      box-shadow: 0 1px 8px rgba(13, 25, 64, 0.12);
    }}
    .lightbox {{
      position: fixed;
      inset: 0;
      background: rgba(4, 8, 22, 0.86);
      z-index: 9999;
      display: flex;
      align-items: flex-start;
      justify-content: center;
      padding: 24px 18px;
      overflow-y: auto;
    }}
    .lightbox[hidden] {{
      display: none;
    }}
    .lightbox-content {{
      width: min(96vw, 1080px);
      flex-shrink: 0;
      margin: 0;
      position: relative;
    }}
    .lightbox img {{
      display: block;
      width: 100%;
      height: auto;
      border-radius: 8px;
      border: 1px solid #2f3854;
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.45);
      background: #fff;
    }}
    .lightbox figcaption {{
      color: #d6dcef;
      margin-top: 8px;
      font-size: 13px;
      line-height: 1.4;
      word-break: break-word;
    }}
    .lightbox-close {{
      position: absolute;
      top: -12px;
      right: -12px;
      width: 34px;
      height: 34px;
      border-radius: 999px;
      border: 1px solid #6d789c;
      background: #1a233d;
      color: #fff;
      font-size: 20px;
      line-height: 1;
      cursor: pointer;
    }}
    .hint {{
      margin: 8px 0 0;
      color: #6f7690;
      font-size: 12px;
    }}
    .empty {{ color: #6f7690; }}
    html {{
      scroll-behavior: smooth;
    }}
    .anchor-offset {{
      scroll-margin-top: 16px;
    }}
  </style>
</head>
<body>
  <h1>網站版本差異報告</h1>
  <div class="meta">
    產生時間：{html.escape(str(payload["generated_at"]))}<br>
    舊版：<code>{html.escape(str(payload["old_root"]))}</code><br>
    新版：<code>{html.escape(str(payload["new_root"]))}</code>
  </div>

  {dep_banner_html}

  <div class="metrics">{summary_html}</div>

  {skip_section_html}

  {text_section_html}

  {image_section_html}

  <section class="section anchor-offset" id="section-page-visual">
    <h2>頁面視覺異動（左右畫面比較）</h2>
    <p class="hint">可點擊任一圖片放大檢視，按 Esc 或點背景關閉。</p>
    {page_visual_html}
  </section>

  {other_section_html}

  {added_section_html}

  {removed_section_html}

  {unchanged_section_html}

  <div class="lightbox" id="image-lightbox" hidden>
    <figure class="lightbox-content">
      <button class="lightbox-close" type="button" aria-label="關閉">×</button>
      <img id="lightbox-image" src="" alt="preview">
      <figcaption id="lightbox-caption"></figcaption>
    </figure>
  </div>
  <script>
    (function () {{
      var lightbox = document.getElementById("image-lightbox");
      var lightboxImage = document.getElementById("lightbox-image");
      var lightboxCaption = document.getElementById("lightbox-caption");
      var closeButton = lightbox.querySelector(".lightbox-close");
      function closeLightbox() {{
        lightbox.hidden = true;
        lightboxImage.src = "";
        lightboxCaption.textContent = "";
      }}
      function openLightbox(src, caption) {{
        lightboxImage.src = src;
        lightboxCaption.textContent = caption || "";
        lightbox.hidden = false;
      }}
      document.querySelectorAll(".js-zoomable").forEach(function (img) {{
        img.addEventListener("click", function () {{
          var src = img.getAttribute("src");
          var caption = img.getAttribute("data-caption") || "";
          if (!src) {{
            return;
          }}
          openLightbox(src, caption);
        }});
      }});
      closeButton.addEventListener("click", closeLightbox);
      lightbox.addEventListener("click", function (event) {{
        if (event.target === lightbox) {{
          closeLightbox();
        }}
      }});
      document.addEventListener("keydown", function (event) {{
        if (event.key === "Escape" && !lightbox.hidden) {{
          closeLightbox();
        }}
      }});
    }})();
  </script>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare two offline website snapshots and generate a visual HTML report."
    )
    parser.add_argument("old_dir", help="Old snapshot folder path")
    parser.add_argument("new_dir", help="New snapshot folder path")
    parser.add_argument(
        "-o",
        "--output",
        default="diff_report",
        help="Output folder for report files (default: diff_report)",
    )
    parser.add_argument(
        "--text-context-lines",
        type=int,
        default=2,
        help="Context lines around text changes in side-by-side view (default: 2)",
    )
    parser.add_argument(
        "--max-text-diff-lines",
        type=int,
        default=300,
        help="Max rows shown for each side-by-side text diff (default: 300)",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable terminal progress bar output.",
    )
    parser.add_argument(
        "--no-page-visual",
        action="store_true",
        help="Disable page-level visual screenshot diff.",
    )
    parser.add_argument(
        "--max-visual-pages",
        type=int,
        default=40,
        help="Max common HTML pages to render for visual diff (default: 40).",
    )
    parser.add_argument(
        "--visual-width",
        type=int,
        default=1366,
        help="Viewport width for page screenshot diff (default: 1366).",
    )
    parser.add_argument(
        "--visual-height",
        type=int,
        default=900,
        help="Viewport height for page screenshot diff (default: 900).",
    )
    parser.add_argument(
        "--visual-wait-ms",
        type=int,
        default=200,
        help="Extra wait time after each page load in ms (default: 200).",
    )
    parser.add_argument(
        "--full-report",
        action="store_true",
        help="Enable full text/image diff sections (default is quick mode).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    old_root = Path(args.old_dir).resolve()
    new_root = Path(args.new_dir).resolve()
    output_dir = Path(args.output).resolve()

    if not old_root.exists() or not old_root.is_dir():
        raise SystemExit(f"old_dir does not exist or is not a folder: {old_root}")
    if not new_root.exists() or not new_root.is_dir():
        raise SystemExit(f"new_dir does not exist or is not a folder: {new_root}")

    report_file = generate_report(
        old_root=old_root,
        new_root=new_root,
        output_dir=output_dir,
        text_context_lines=max(0, args.text_context_lines),
        max_text_diff_lines=max(20, args.max_text_diff_lines),
        show_progress=not args.no_progress,
        page_visual_enabled=not args.no_page_visual,
        page_visual_max_pages=max(0, args.max_visual_pages),
        page_visual_width=max(200, args.visual_width),
        page_visual_height=max(200, args.visual_height),
        page_visual_wait_ms=max(0, args.visual_wait_ms),
        quick_mode=not args.full_report,
    )

    print(f"Done. Report generated: {report_file}")
    if not PIL_AVAILABLE:
        print("Tip: install Pillow for image pixel highlight: pip install pillow")


if __name__ == "__main__":
    main()
