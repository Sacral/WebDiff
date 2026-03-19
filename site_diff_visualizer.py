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
import base64
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
from typing import Dict, List, Optional, Sequence, Set, Tuple

try:
    from PIL import Image, ImageChops, ImageStat

    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

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


def _save_image_diff(
    old_path: Path,
    new_path: Path,
    diff_overlay_path: Path,
) -> Optional[Dict[str, object]]:
    if not PIL_AVAILABLE:
        return None

    old_img = Image.open(old_path).convert("RGBA")
    new_img = Image.open(new_path).convert("RGBA")

    if old_img.size != new_img.size:
        return {
            "same_size": False,
            "size_old": old_img.size,
            "size_new": new_img.size,
            "changed_ratio": 1.0,
        }

    diff = ImageChops.difference(old_img, new_img)
    stat = ImageStat.Stat(diff)
    total_pixels = old_img.size[0] * old_img.size[1]

    # Mean value range per channel is 0..255.
    mean_delta = sum(stat.mean[:3]) / 3.0

    diff_mask = diff.convert("L")
    hist = diff_mask.histogram()
    non_zero = total_pixels - (hist[0] if hist else 0)
    changed_ratio = non_zero / max(1, total_pixels)

    # Highlight changed pixels in red over new image.
    mask = diff.convert("L").point(lambda x: 255 if x > 10 else 0)
    overlay = new_img.copy()
    red_layer = Image.new("RGBA", new_img.size, (255, 0, 0, 150))
    overlay.paste(red_layer, (0, 0), mask)

    diff_overlay_path.parent.mkdir(parents=True, exist_ok=True)
    overlay.save(diff_overlay_path)

    return {
        "same_size": True,
        "size_old": old_img.size,
        "size_new": new_img.size,
        "mean_delta": round(mean_delta, 3),
        "changed_ratio": round(changed_ratio, 6),
    }


def _safe_anchor(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


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
        }, (
            "Playwright not installed; skipped page visual diff."
        )

    old_files = collect_files(old_root)
    new_files = collect_files(new_root)
    candidates = [
        rel
        for rel in sorted(set(old_files) & set(new_files))
        if Path(rel).suffix.lower() in {".html", ".htm"}
    ]
    if max_pages > 0:
        candidates = candidates[:max_pages]

    if not candidates:
        return [], {
            "total_candidates": 0,
            "compared": 0,
            "changed": 0,
            "skipped": 0,
            "http_error_pages": 0,
            "http_error_requests": 0,
        }, None

    old_server = LocalSnapshotServer(old_root)
    new_server = LocalSnapshotServer(new_root)
    old_server.start()
    new_server.start()

    items: List[Dict[str, object]] = []
    stats = {
        "total_candidates": len(candidates),
        "compared": 0,
        "changed": 0,
        "skipped": 0,
        "http_error_pages": 0,
        "http_error_requests": 0,
    }
    progress = ProgressBar(total=len(candidates), enabled=show_progress, label="Rendering pages")
    warning: Optional[str] = None

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except PlaywrightError as exc:
                message = str(exc)
                if "Executable doesn't exist" not in message:
                    raise
                browser = None
                for channel in ("msedge", "chrome"):
                    try:
                        browser = p.chromium.launch(headless=True, channel=channel)
                        break
                    except PlaywrightError:
                        continue
                if browser is None:
                    warning = (
                        "Playwright browser is missing. Install with "
                        "`python -m playwright install chromium`, or ensure Edge/Chrome is installed."
                    )
                    return items, stats, warning
            context = browser.new_context(
                viewport={"width": max(200, viewport_width), "height": max(200, viewport_height)},
                ignore_https_errors=True,
            )
            page = context.new_page()
            current_rel: Dict[str, Optional[str]] = {"value": None}
            per_page_http_errors: Dict[str, Dict[str, object]] = {}

            def on_response(response) -> None:
                rel = current_rel.get("value")
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

            page.on("response", on_response)

            for rel in candidates:
                try:
                    current_rel["value"] = rel
                    quoted_rel = urllib.parse.quote(rel, safe="/")
                    old_url = f"http://127.0.0.1:{old_server.port}/{quoted_rel}"
                    new_url = f"http://127.0.0.1:{new_server.port}/{quoted_rel}"
                    anchor = _safe_anchor(f"page::{rel}")
                    old_preview = Path("assets") / f"{anchor}_page_old.png"
                    new_preview = Path("assets") / f"{anchor}_page_new.png"
                    overlay_preview = Path("assets") / f"{anchor}_page_overlay.png"

                    page.goto(old_url, wait_until="networkidle", timeout=120000)
                    page.wait_for_timeout(max(0, wait_ms))
                    page.screenshot(path=str(output_dir / old_preview), full_page=True)

                    page.goto(new_url, wait_until="networkidle", timeout=120000)
                    page.wait_for_timeout(max(0, wait_ms))
                    page.screenshot(path=str(output_dir / new_preview), full_page=True)

                    stats["compared"] += 1
                    old_hash = sha1_file(output_dir / old_preview)
                    new_hash = sha1_file(output_dir / new_preview)
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

                    metrics = _save_image_diff(
                        output_dir / old_preview,
                        output_dir / new_preview,
                        output_dir / overlay_preview,
                    )
                    stats["changed"] += 1
                    items.append(
                        {
                            "path": rel,
                            "old_hash": old_hash[:10],
                            "new_hash": new_hash[:10],
                            "old_preview": old_preview.as_posix(),
                            "new_preview": new_preview.as_posix(),
                            "overlay_preview": overlay_preview.as_posix() if metrics else "",
                            "metrics": metrics or {"note": "Pillow not installed; only screenshot compared."},
                            "http_error_count": error_count,
                            "http_error_samples": error_samples,
                        }
                    )
                except Exception:
                    stats["skipped"] += 1
                finally:
                    current_rel["value"] = None
                    progress.update()

            browser.close()
    except PlaywrightError as exc:
        warning = f"Playwright failed; skipped page visual diff. {exc}"
    finally:
        progress.finish()
        old_server.stop()
        new_server.stop()

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
        "modified_page_visual": 0,
        "modified_other": 0,
        "unchanged": 0,
    }
    text_items: List[Dict[str, object]] = []
    image_items: List[Dict[str, object]] = []
    other_items: List[Dict[str, object]] = []
    added_items: List[str] = []
    removed_items: List[str] = []
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
                        "overlay_preview": overlay_preview.as_posix() if metrics else "",
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
    }
    (output_dir / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    html_report = render_html(payload)
    report_file = output_dir / "report.html"
    report_file.write_text(html_report, encoding="utf-8")
    return report_file


def _render_summary_card(summary: Dict[str, int]) -> str:
    parts = []
    for key, label, anchor in [
        ("added", "新增", "section-added"),
        ("removed", "刪除", "section-removed"),
        ("modified_text", "文字異動", "section-text"),
        ("modified_image", "圖片異動", "section-image"),
        ("modified_page_visual", "頁面視覺異動", "section-page-visual"),
        ("modified_other", "其他異動", "section-other"),
        ("unchanged", "未變更", "section-unchanged"),
    ]:
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


def _render_image_items(items: Sequence[Dict[str, object]]) -> str:
    if not items:
        return "<p class='empty'>沒有偵測到圖片異動。</p>"
    chunks: List[str] = []
    for item in items:
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
            f"<div><h4>差異高亮</h4><img src='{html.escape(str(overlay))}' alt='overlay'></div>"
            if overlay
            else "<div><h4>差異高亮</h4><p class='empty'>未產生（請安裝 Pillow）</p></div>"
        )
        chunks.append(
            "<details class='card'>"
            f"<summary><code>{html.escape(str(item['path']))}</code> "
            f"<span class='hash'>[{item['old_hash']} -> {item['new_hash']}]</span></summary>"
            "<div class='img-grid'>"
            f"<div><h4>舊版</h4><img src='{html.escape(str(item['old_preview']))}' alt='old'></div>"
            f"<div><h4>新版</h4><img src='{html.escape(str(item['new_preview']))}' alt='new'></div>"
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
            f"<div><h4>差異高亮</h4><img src='{html.escape(str(overlay))}' alt='overlay'></div>"
            if overlay
            else "<div><h4>差異高亮</h4><p class='empty'>未產生（請安裝 Pillow）</p></div>"
        )
        chunks.append(
            "<details class='card'>"
            f"<summary><code>{html.escape(str(item['path']))}</code> "
            f"<span class='hash'>[{item['old_hash']} -> {item['new_hash']}]</span>"
            f" <span class='chip chip-del'>fail {int(item.get('http_error_count', 0))}</span></summary>"
            "<div class='img-grid page-grid'>"
            f"<div><h4>舊版畫面</h4><img src='{html.escape(str(item['old_preview']))}' alt='old-page'></div>"
            f"<div><h4>新版畫面</h4><img src='{html.escape(str(item['new_preview']))}' alt='new-page'></div>"
            f"{overlay_block}"
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


def render_html(payload: Dict[str, object]) -> str:
    summary_html = _render_summary_card(payload["summary"])  # type: ignore[index]
    text_html = _render_text_items(payload["text_items"])  # type: ignore[index]
    image_html = _render_image_items(payload["image_items"])  # type: ignore[index]
    page_visual_html = _render_page_visual_items(
        payload.get("page_visual_items", []),  # type: ignore[arg-type]
        payload.get("page_visual_stats", {}),  # type: ignore[arg-type]
        str(payload.get("page_visual_warning", "")),
    )
    other_html = _render_other_items(payload["other_items"])  # type: ignore[index]
    added_html = _render_simple_list(payload["added_items"], "沒有新增檔案。")  # type: ignore[index]
    removed_html = _render_simple_list(payload["removed_items"], "沒有刪除檔案。")  # type: ignore[index]

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
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 12px;
      margin-top: 8px;
    }}
    .img-grid img {{
      max-width: 100%;
      border: 1px solid #dce1ef;
      border-radius: 6px;
      background: white;
    }}
    .page-grid img {{
      box-shadow: 0 1px 8px rgba(13, 25, 64, 0.12);
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

  <div class="metrics">{summary_html}</div>

  <section class="section anchor-offset" id="section-text">
    <h2>文字異動</h2>
    {text_html}
  </section>

  <section class="section anchor-offset" id="section-image">
    <h2>圖片異動</h2>
    {image_html}
  </section>

  <section class="section anchor-offset" id="section-page-visual">
    <h2>頁面視覺異動（左右畫面比較）</h2>
    {page_visual_html}
  </section>

  <section class="section anchor-offset" id="section-other">
    <h2>其他二進位檔異動</h2>
    {other_html}
  </section>

  <section class="section anchor-offset" id="section-added">
    <h2>新增檔案</h2>
    {added_html}
  </section>

  <section class="section anchor-offset" id="section-removed">
    <h2>刪除檔案</h2>
    {removed_html}
  </section>

  <section class="section anchor-offset" id="section-unchanged">
    <h2>未變更統計</h2>
    <p>未變更檔案數：<strong>{payload["summary"].get("unchanged", 0)}</strong></p>
  </section>
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
        default=800,
        help="Extra wait time after each page load in ms (default: 800).",
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
    )

    print(f"Done. Report generated: {report_file}")
    if not PIL_AVAILABLE:
        print("Tip: install Pillow for image pixel highlight: pip install pillow")


if __name__ == "__main__":
    main()
