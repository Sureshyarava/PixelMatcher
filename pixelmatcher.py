#!/usr/bin/env python3
"""
PixelMatcher — AEM sitemap visual regression testing (strip screenshots + HTML report).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html as html_module
import io
import re
import shutil
import signal
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from math import ceil
from pathlib import Path
from typing import Any

import numpy as np
import requests
from PIL import Image, ImageFilter

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None  # type: ignore
    PlaywrightError = Exception  # type: ignore

SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"


@dataclass(frozen=True)
class PatrolConfig:
    """Capture + diff + report settings (single run)."""

    viewport_width: int = 1440
    strip_height: int = 900
    max_width_capture: int = 1440
    jpeg_quality: int = 85
    full_page_max_css_height: int = 40000
    device_scale_factor: float = 1.0
    diff_pixel_threshold: float = 15.0
    diff_blur_radius: float = 0.0
    report_jpeg_quality: int = 80
    report_thumb_max_width: int = 800
# document.body.scrollHeight alone is often ~viewport height; use root + body max.
_PAGE_SCROLL_HEIGHT_JS = """
() => {
  const r = document.documentElement;
  const b = document.body;
  const n = (x) => (x == null || x === 0 ? 0 : x);
  return Math.max(
    1,
    r.scrollHeight,
    n(b && b.scrollHeight),
    r.offsetHeight,
    n(b && b.offsetHeight)
  );
}
"""
SITEMAP_MAX_DEPTH = 10
OVERLAY_COLOR = np.array([255, 59, 48], dtype=np.float32)
OVERLAY_ALPHA = 180

_shutdown = threading.Event()
_compare_mode_active = False
_progress_lock = threading.Lock()
_progress_done = 0


def _signal_handler(signum: int, frame: Any) -> None:
    if not _shutdown.is_set():
        if _compare_mode_active:
            print("\n[PixelMatcher] Interrupted — generating partial report...", flush=True)
        else:
            print("\n[PixelMatcher] Interrupted — stopping capture.", flush=True)
    _shutdown.set()


def sitemap_slug_from_url(sitemap_url: str) -> str:
    from urllib.parse import urlparse

    host = urlparse(sitemap_url).netloc or "unknown"
    return re.sub(r"[^a-zA-Z0-9]+", "_", host).strip("_") or "site"


def url_to_page_slug(url: str) -> str:
    from urllib.parse import urlparse, unquote

    p = urlparse(url)
    path = unquote((p.path or "/").strip("/"))
    base = path.replace("/", "_") if path else "index"
    if p.query:
        base = f"{base}_{re.sub(r'[^a-zA-Z0-9_-]+', '_', p.query)[:64]}"
    base = re.sub(r"_+", "_", base).strip("_")
    if not base:
        base = "index"
    short = hashlib.md5(url.encode("utf-8")).hexdigest()[:10]
    combined = f"{base[:100]}_{short}"
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", combined).strip("_")


def _fetch_xml(url: str) -> bytes:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content


def collect_page_urls_from_sitemap(sitemap_url: str, depth: int = 0, seen: set[str] | None = None) -> list[str]:
    if seen is None:
        seen = set()
    if sitemap_url in seen or depth > SITEMAP_MAX_DEPTH:
        return []
    seen.add(sitemap_url)

    content = _fetch_xml(sitemap_url)
    root = ET.fromstring(content)
    # AEM uses http://www.sitemaps.org/schemas/sitemap/0.9 — tags are {ns}loc etc.; use sm: prefix.
    ns = {"sm": SITEMAP_NS}
    tag_local = root.tag.split("}")[-1] if "}" in root.tag else root.tag

    if tag_local == "sitemapindex":
        out: list[str] = []
        for sm in root.findall("sm:sitemap", ns):
            loc = sm.find("sm:loc", ns)
            if loc is not None and loc.text:
                out.extend(collect_page_urls_from_sitemap(loc.text.strip(), depth + 1, seen))
        if not out:
            for loc in root.findall(".//{*}loc"):
                if loc.text:
                    u = loc.text.strip()
                    if u != sitemap_url and u.endswith(".xml"):
                        out.extend(collect_page_urls_from_sitemap(u, depth + 1, seen))
        return out

    urls: list[str] = []
    for loc in root.findall("sm:url/sm:loc", ns):
        if loc.text:
            urls.append(loc.text.strip())
    if not urls:
        for url_el in root.findall(".//{*}url"):
            loc = url_el.find("{*}loc")
            if loc is None:
                loc = url_el.find("loc")
            if loc is not None and loc.text:
                urls.append(loc.text.strip())
    return urls


def parse_sitemap(sitemap_url: str) -> list[str]:
    urls = collect_page_urls_from_sitemap(sitemap_url, 0, set())
    seen_pages: set[str] = set()
    unique: list[str] = []
    for u in urls:
        if u not in seen_pages:
            seen_pages.add(u)
            unique.append(u)
    return unique


def resize_to_max_width(img: Image.Image, max_w: int) -> Image.Image:
    w, h = img.size
    if w <= max_w:
        return img
    nh = int(round(h * (max_w / w)))
    return img.resize((max_w, nh), Image.Resampling.LANCZOS)


def measure_scroll_height(page: Any) -> int:
    return int(page.evaluate(_PAGE_SCROLL_HEIGHT_JS))


def resolve_full_page_height(page: Any) -> int:
    """
    Full height after layout. Scrolls to the bottom a few times so lazy/infinite
    content can expand (AEM, images, etc.), then returns stable scroll height.
    """
    prev = -1
    for _ in range(10):
        h = measure_scroll_height(page)
        if h == prev:
            return max(1, h)
        prev = h
        page.evaluate(
            "() => window.scrollTo(0, Math.max("
            "document.documentElement.scrollHeight, "
            "document.body ? document.body.scrollHeight : 0))"
        )
        time.sleep(0.4)
    return max(1, prev)


def scroll_page_for_lazy_load(page: Any, pause_s: float = 0.18) -> None:
    """
    Scroll through the document in steps so below-the-fold images and lazy blocks load.
    One URL is always captured sequentially in a single page (no parallel strips).
    """
    for _ in range(400):
        r = page.evaluate(
            """() => {
          const se = document.scrollingElement || document.documentElement;
          const max = Math.max(0, se.scrollHeight - se.clientHeight);
          const cur = se.scrollTop;
          const step = Math.max(200, Math.floor(se.clientHeight * 0.88));
          if (cur >= max - 2) return { done: true };
          se.scrollTop = Math.min(cur + step, max);
          return { done: false };
        }"""
        )
        time.sleep(pause_s)
        if r and r.get("done"):
            break
    page.evaluate(
        """() => {
      const se = document.scrollingElement || document.documentElement;
      se.scrollTop = Math.max(0, se.scrollHeight - se.clientHeight);
    }"""
    )
    time.sleep(0.45)


def wait_for_images_and_fonts(page: Any) -> None:
    try:
        page.evaluate(
            """async () => {
          if (document.fonts && document.fonts.ready) await document.fonts.ready;
          const imgs = Array.from(document.images);
          await Promise.all(
            imgs.map(
              (img) =>
                img.complete
                  ? Promise.resolve()
                  : new Promise((r) => {
                      img.onload = img.onerror = () => r();
                      setTimeout(r, 5000);
                    })
            )
          );
        }"""
        )
    except Exception:
        pass


def ensure_page_ready_for_capture(page: Any) -> None:
    """
    Load deferred content before measuring height or taking screenshots.
    Call after page.goto(..., wait_until='networkidle').
    """
    time.sleep(0.2)
    scroll_page_for_lazy_load(page)
    wait_for_images_and_fonts(page)
    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:
        try:
            page.wait_for_load_state("load", timeout=10_000)
        except Exception:
            pass
    time.sleep(0.35)
    scroll_page_for_lazy_load(page)
    wait_for_images_and_fonts(page)
    time.sleep(0.25)


def save_strip_jpeg(img: Image.Image, path: Path, quality: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.save(path, "JPEG", quality=quality, optimize=True)


def _scroll_reset_top(page: Any) -> None:
    page.evaluate(
        """() => {
      window.scrollTo(0, 0);
      const se = document.scrollingElement;
      if (se) se.scrollTop = 0;
      if (document.body) document.body.scrollTop = 0;
    }"""
    )


def _capture_strips_via_full_page_split(
    page: Any,
    out_dir: Path,
    page_slug: str,
    full_height: int,
    cfg: PatrolConfig,
) -> bool:
    """
    One full-page screenshot, then crop vertical bands by CSS-pixel ranges.
    Avoids duplicate strips when window.scrollTo does not move the viewport (inner scrollers).
    """
    try:
        img_bytes = page.screenshot(
            type="jpeg",
            full_page=True,
            quality=cfg.jpeg_quality,
            scale="css",
        )
        full_img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    except Exception:
        return False
    iw, ih = full_img.size
    if iw < 2 or ih < 2:
        return False
    # With scale=css, bitmap height should match measured document height. If the
    # screenshot is much shorter, lazy content did not paint — use viewport strips.
    if full_height > cfg.strip_height * 2 and ih < full_height * 0.88:
        return False
    ratio = ih / float(max(1, full_height))
    sh = cfg.strip_height
    num_strips = max(1, ceil(full_height / sh))
    y1_probe = int(round(min(sh, full_height) * ratio))
    if num_strips > 1 and y1_probe < 24:
        return False
    for i in range(num_strips):
        if _shutdown.is_set():
            break
        css_y0 = i * sh
        css_y1 = min((i + 1) * sh, full_height)
        y0 = int(round(css_y0 * ratio))
        y1 = int(min(round(css_y1 * ratio), ih))
        if y1 <= y0:
            y1 = min(y0 + 1, ih)
        if y0 >= ih:
            break
        strip = full_img.crop((0, y0, iw, y1))
        strip = resize_to_max_width(strip, cfg.max_width_capture)
        save_strip_jpeg(strip, out_dir / f"strip_{i:03d}.jpg", cfg.jpeg_quality)
    return True


def _capture_strips_via_viewport_scroll(
    page: Any,
    out_dir: Path,
    full_height: int,
    cfg: PatrolConfig,
) -> None:
    """Scroll to each strip position and capture the live viewport.

    out_dir is the per-page slug directory; files are saved as strip_000.jpg.
    Scroll JS forces instant positioning (bypasses smooth-scroll CSS) and tries
    every scroll target in order so it works on CNN, AEM, and similar sites that
    override the default scrolling element.
    """
    sh = cfg.strip_height
    num_strips = max(1, ceil(full_height / sh))
    for i in range(num_strips):
        if _shutdown.is_set():
            break
        is_last = (i == num_strips - 1)
        # For the last strip scroll to the absolute bottom (999999) so any
        # content that loaded during the loop (footers, sticky bars) is visible.
        # For all other strips use the exact calculated y position.
        scroll_target = 999999 if is_last else i * sh
        page.evaluate(
            f"""() => {{
          const y = {scroll_target};
          // Force instant scroll — bypasses smooth-scroll CSS on CNN et al.
          const root = document.documentElement;
          const prev = root.style.scrollBehavior;
          root.style.scrollBehavior = 'auto';
          if (document.body) document.body.style.scrollBehavior = 'auto';
          // Try every possible scroll host in order of reliability.
          window.scrollTo(0, y);
          root.scrollTop = y;
          if (document.body) document.body.scrollTop = y;
          const se = document.scrollingElement;
          if (se && se !== root && se !== document.body) se.scrollTop = y;
          root.style.scrollBehavior = prev;
        }}"""
        )
        time.sleep(0.3)
        # Always capture a full strip_height viewport for the last strip so the
        # footer is never clipped.  Non-last strips use the exact remaining height.
        clip_h = sh if is_last else min(sh, full_height - i * sh)
        if clip_h <= 0:
            break
        clip = {"x": 0, "y": 0, "width": cfg.viewport_width, "height": clip_h}
        img_bytes = page.screenshot(
            type="jpeg",
            clip=clip,
            full_page=False,
            quality=cfg.jpeg_quality,
        )
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img = resize_to_max_width(img, cfg.max_width_capture)
        save_strip_jpeg(img, out_dir / f"strip_{i:03d}.jpg", cfg.jpeg_quality)


def capture_page_strips(
    browser: Any,
    url: str,
    screenshots_dir: Path,
    page_slug: str,
    cfg: PatrolConfig,
) -> tuple[str, str | None]:
    # Realistic browser context — reduces bot-detection rejections on sites
    # like news publishers that check User-Agent and language headers.
    context = browser.new_context(
        viewport={"width": cfg.viewport_width, "height": cfg.strip_height},
        device_scale_factor=cfg.device_scale_factor,
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        timezone_id="America/New_York",
        java_script_enabled=True,
    )
    # Hide the webdriver fingerprint used by many bot-detection libraries.
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    page = context.new_page()
    # Per-page subdirectory: screenshots_dir/<slug>/strip_000.jpg
    page_dir = screenshots_dir / page_slug
    page_dir.mkdir(parents=True, exist_ok=True)
    try:
        # networkidle is best for AEM/React pages; fall back to domcontentloaded
        # for news sites with continuous ad/analytics polling that never idle.
        try:
            page.goto(url, wait_until="networkidle", timeout=30_000)
        except PlaywrightError:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=30_000)
            except Exception:
                pass
        time.sleep(0.5)  # let initial render settle

        # Pre-scroll to the absolute bottom so lazy-loaded footers and AEM
        # components have painted before we measure page height.  Without this,
        # footers that live outside the initial scrollHeight are silently clipped
        # from the last strip.
        page.evaluate("""() => {
            const el = document.documentElement;
            el.style.scrollBehavior = 'auto';
            if (document.body) document.body.style.scrollBehavior = 'auto';
            window.scrollTo(0, 999999);
            el.scrollTop = 999999;
            if (document.body) document.body.scrollTop = 999999;
        }""")
        time.sleep(0.5)  # let footer / sticky components fully render
        full_height = max(cfg.strip_height, measure_scroll_height(page))

        # Scroll back to the top before the strip loop begins.
        page.evaluate("""() => {
            window.scrollTo(0, 0);
            document.documentElement.scrollTop = 0;
            if (document.body) document.body.scrollTop = 0;
        }""")
        time.sleep(0.3)

        # Viewport-scroll: scroll to each position and capture the live viewport.
        # Avoids the full-page-screenshot issue where lazy-loaded images get
        # evicted from browser memory when scrolling back to top.
        _capture_strips_via_viewport_scroll(page, page_dir, full_height, cfg)
        return ("OK", None)
    except PlaywrightError as e:
        return ("ERROR", str(e) or "Playwright error")
    except Exception as e:
        return ("ERROR", str(e))
    finally:
        context.close()


def worker_capture_chunk(
    urls: list[str],
    screenshots_dir: Path,
    slug_map: dict[str, str],
    total_urls: int,
    cfg: PatrolConfig,
) -> list[dict[str, Any]]:
    if sync_playwright is None:
        return [{"url": u, "status": "ERROR", "error": "playwright not installed", "slug": slug_map[u]} for u in urls]

    results: list[dict[str, Any]] = []
    global _progress_done
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            for url in urls:
                if _shutdown.is_set():
                    results.append(
                        {
                            "url": url,
                            "status": "ABORT",
                            "error": "shutdown",
                            "slug": slug_map[url],
                        }
                    )
                    continue
                slug = slug_map[url]
                st, err = capture_page_strips(browser, url, screenshots_dir, slug, cfg)
                rec = {"url": url, "status": st, "error": err, "slug": slug}
                results.append(rec)
                with _progress_lock:
                    _progress_done += 1
                    n = _progress_done
                if n % 10 == 0 or n == total_urls:
                    print(f"[PixelMatcher] Progress: {n}/{total_urls}...", flush=True)
        finally:
            browser.close()
    return results


def run_screenshots(
    urls: list[str],
    screenshots_dir: Path,
    workers: int,
    cfg: PatrolConfig,
) -> list[dict[str, Any]]:
    global _progress_done
    _progress_done = 0
    slug_map = {u: url_to_page_slug(u) for u in urls}
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    n_workers = max(1, min(workers, len(urls)))
    if not urls:
        return []

    chunks: list[list[str]] = [[] for _ in range(n_workers)]
    for i, u in enumerate(urls):
        chunks[i % n_workers].append(u)

    total_urls = len(urls)
    all_results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        futs = [
            ex.submit(worker_capture_chunk, ch, screenshots_dir, slug_map, total_urls, cfg)
            for ch in chunks
            if ch
        ]
        for fut in as_completed(futs):
            all_results.extend(fut.result())

    by_url = {r["url"]: r for r in all_results}
    return [by_url[u] for u in urls if u in by_url]


def list_strips_for_slug(directory: Path, slug: str) -> list[Path]:
    """Return sorted strip paths from directory/<slug>/strip_*.jpg."""
    slug_dir = directory / slug
    if not slug_dir.is_dir():
        return []
    return sorted(slug_dir.glob("strip_*.jpg"), key=lambda p: p.name)


def strip_index_from_name(path: Path) -> int:
    m = re.search(r"strip_(\d+)\.jpg$", path.name)
    return int(m.group(1)) if m else -1


def max_strip_index(slug: str, directory: Path) -> int:
    slug_dir = directory / slug
    m = -1
    for p in slug_dir.glob("strip_*.jpg"):
        m = max(m, strip_index_from_name(p))
    return m


def sorted_strip_paths(directory: Path, slug: str) -> list[Path]:
    slug_dir = directory / slug
    if not slug_dir.is_dir():
        return []
    paths = [p for p in slug_dir.glob("strip_*.jpg") if p.is_file()]
    return sorted(paths, key=strip_index_from_name)


def strip_scroll_label(idx: int, strip_height: int) -> str:
    y0 = idx * strip_height
    y1 = y0 + strip_height
    return f"Section {idx} (~{y0}px–{y1}px doc scroll)"


def load_image_float(path: Path) -> np.ndarray:
    img = Image.open(path).convert("RGB")
    return np.asarray(img, dtype=np.float32)


def compare_strips(
    baseline_path: Path,
    current_path: Path,
    threshold: float,
    cfg: PatrolConfig,
) -> tuple[bool, float, Image.Image | None]:
    """
    Compare at full stored resolution (LANCZOS resize current → baseline size).
    Uses per-channel max diff: a pixel is changed if any channel differs by more
    than cfg.diff_pixel_threshold. Optional Gaussian blur suppresses JPEG ringing.
    """
    base = load_image_float(baseline_path)
    cur_img = Image.open(current_path).convert("RGB")
    cur_img = cur_img.resize((base.shape[1], base.shape[0]), Image.Resampling.LANCZOS)
    cur = np.asarray(cur_img, dtype=np.float32)
    if cfg.diff_blur_radius > 0:
        b_u8 = np.clip(base, 0, 255).astype(np.uint8)
        c_u8 = np.clip(cur, 0, 255).astype(np.uint8)
        b_pil = Image.fromarray(b_u8).filter(ImageFilter.GaussianBlur(radius=cfg.diff_blur_radius))
        c_pil = Image.fromarray(c_u8).filter(ImageFilter.GaussianBlur(radius=cfg.diff_blur_radius))
        base = np.asarray(b_pil, dtype=np.float32)
        cur = np.asarray(c_pil, dtype=np.float32)
    diff = np.abs(base - cur)
    changed = diff.max(axis=2) > cfg.diff_pixel_threshold
    total = changed.size
    pct = float(changed.sum()) / float(total) if total else 0.0
    ok = pct <= threshold
    overlay_img: Image.Image | None = None
    if not ok:
        cur_u8 = np.asarray(cur_img, dtype=np.float32)
        alpha = OVERLAY_ALPHA / 255.0
        mask = changed[..., np.newaxis].astype(np.float32)
        blended = cur_u8 * (1.0 - mask * alpha) + OVERLAY_COLOR * (mask * alpha)
        overlay_img = Image.fromarray(np.clip(blended, 0, 255).astype(np.uint8), "RGB")
    return ok, pct, overlay_img


def image_to_report_b64(path: Path | None, cfg: PatrolConfig) -> str:
    if path is None or not path.exists():
        return ""
    img = Image.open(path).convert("RGB")
    img = resize_to_max_width(img, cfg.report_thumb_max_width)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=cfg.report_jpeg_quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def pil_to_report_b64(img: Image.Image, cfg: PatrolConfig) -> str:
    """Encode an in-memory PIL image to base64 for embedding in the HTML report."""
    img = resize_to_max_width(img.convert("RGB"), cfg.report_thumb_max_width)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=cfg.report_jpeg_quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def baseline_has_files(baseline_dir: Path) -> bool:
    return baseline_dir.is_dir() and any(baseline_dir.glob("*/strip_*.jpg"))


@dataclass
class StripOutcome:
    index: int
    kind: str
    passed: bool
    pct: float
    baseline_path: Path | None
    current_path: Path | None
    diff_b64: str = ""   # base64-encoded diff overlay — no file written to disk
    label: str = ""


@dataclass
class PageOutcome:
    url: str
    slug: str
    status: str
    error: str | None = None
    strips: list[StripOutcome] = field(default_factory=list)
    max_pct: float = 0.0
    strip_count: int = 0
    changed_count: int = 0


def run_compare(
    baseline_dir: Path,
    run_screenshots_dir: Path,
    capture_results: list[dict[str, Any]],
    threshold: float,
    cfg: PatrolConfig,
) -> list[PageOutcome]:
    outcomes: list[PageOutcome] = []

    for r in capture_results:
        url = r["url"]
        slug = r["slug"]
        if r["status"] == "ABORT":
            outcomes.append(PageOutcome(url=url, slug=slug, status="ERROR", error="Interrupted"))
            continue
        if r["status"] == "ERROR":
            outcomes.append(PageOutcome(url=url, slug=slug, status="ERROR", error=r.get("error")))
            continue

        base_files = list_strips_for_slug(baseline_dir, slug)
        cur_files = list_strips_for_slug(run_screenshots_dir, slug)

        if not base_files:
            outcomes.append(
                PageOutcome(
                    url=url,
                    slug=slug,
                    status="NEW",
                    strip_count=len(cur_files),
                    max_pct=0.0,
                    changed_count=0,
                )
            )
            continue

        ma = max_strip_index(slug, baseline_dir)
        mb = max_strip_index(slug, run_screenshots_dir)
        max_i = max(ma, mb)
        strips_out: list[StripOutcome] = []
        failed = False
        max_pct = 0.0
        changed = 0
        sh = cfg.strip_height

        for i in range(max_i + 1):
            bp = baseline_dir / slug / f"strip_{i:03d}.jpg"
            cp = run_screenshots_dir / slug / f"strip_{i:03d}.jpg"
            b_exists = bp.exists()
            c_exists = cp.exists()

            if not b_exists and c_exists:
                strips_out.append(
                    StripOutcome(
                        i,
                        "NEW_SECTION",
                        False,
                        1.0,
                        None,
                        cp,
                        None,
                        label=f"Section {i} ({i * sh}px–{(i + 1) * sh}px) — new",
                    )
                )
                failed = True
                changed += 1
                max_pct = max(max_pct, 1.0)
            elif b_exists and not c_exists:
                strips_out.append(
                    StripOutcome(
                        i,
                        "REMOVED",
                        False,
                        1.0,
                        bp,
                        None,
                        None,
                        label=f"Section {i} ({i * sh}px–{(i + 1) * sh}px) — removed",
                    )
                )
                failed = True
                changed += 1
                max_pct = max(max_pct, 1.0)
            elif b_exists and c_exists:
                ok, pct, overlay = compare_strips(bp, cp, threshold, cfg)
                max_pct = max(max_pct, pct)
                diff_b64 = ""
                if not ok:
                    changed += 1
                    failed = True
                    if overlay:
                        diff_b64 = pil_to_report_b64(overlay, cfg)
                strips_out.append(
                    StripOutcome(
                        i,
                        "DIFF",
                        ok,
                        pct,
                        bp,
                        cp,
                        diff_b64,
                        label=f"Section {i} ({i * sh}px–{(i + 1) * sh}px)",
                    )
                )

        st = "FAIL" if failed else "PASS"
        outcomes.append(
            PageOutcome(
                url=url,
                slug=slug,
                status=st,
                strips=strips_out,
                max_pct=max_pct,
                strip_count=max_i + 1,
                changed_count=changed,
            )
        )

    return outcomes


def merge_new_baselines(baseline_dir: Path, run_screenshots_dir: Path, outcomes: list[PageOutcome]) -> None:
    baseline_dir.mkdir(parents=True, exist_ok=True)
    for o in outcomes:
        if o.status != "NEW":
            continue
        src = run_screenshots_dir / o.slug
        if not src.is_dir():
            continue
        dest = baseline_dir / o.slug
        dest.mkdir(parents=True, exist_ok=True)
        for p in sorted(src.glob("strip_*.jpg")):
            shutil.copy2(p, dest / p.name)


def promote_run_to_baseline(run_screenshots_dir: Path, baseline_dir: Path) -> None:
    baseline_dir.mkdir(parents=True, exist_ok=True)
    for slug_dir in run_screenshots_dir.iterdir():
        if not slug_dir.is_dir():
            continue
        dest = baseline_dir / slug_dir.name
        dest.mkdir(parents=True, exist_ok=True)
        for p in slug_dir.glob("strip_*.jpg"):
            shutil.copy2(p, dest / p.name)


def build_html_report(
    sitemap_url: str,
    ts: str,
    outcomes: list[PageOutcome],
    run_screenshots_dir: Path,
    aborted: bool,
    cfg: PatrolConfig,
) -> str:
    total = len(outcomes)
    passed = sum(1 for o in outcomes if o.status == "PASS")
    failed = sum(1 for o in outcomes if o.status == "FAIL")
    errors = sum(1 for o in outcomes if o.status == "ERROR")
    new_pages = sum(1 for o in outcomes if o.status == "NEW")

    def sort_key(o: PageOutcome) -> tuple[int, str]:
        order = {"FAIL": 0, "NEW": 1, "PASS": 2, "ERROR": 3}
        return (order.get(o.status, 9), o.url)

    sorted_pages = sorted(outcomes, key=sort_key)

    parts: list[str] = []
    parts.append("<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><title>PixelMatcher</title>")
    parts.append(
        "<style>"
        "body{font-family:system-ui,-apple-system,sans-serif;background:#0f0f0f;color:#e5e5e5;margin:0;padding:0;}"
        "header{background:#111;padding:16px 24px;border-bottom:1px solid #333;}"
        "h1{margin:0 0 8px;font-size:1.25rem;}"
        ".meta{font-size:0.85rem;color:#aaa;margin-bottom:12px;}"
        ".chips span{display:inline-block;margin-right:10px;padding:4px 10px;border-radius:6px;background:#222;font-size:0.85rem;}"
        ".wrap{padding:20px;max-width:1400px;margin:0 auto;}"
        ".card{background:#1a1a1a;border-radius:8px;padding:16px;margin-bottom:16px;border-left:4px solid #636366;}"
        ".card.fail{border-left-color:#FF3B30;}"
        ".card.pass{border-left-color:#30D158;}"
        ".card.err{border-left-color:#636366;}"
        ".card.new{border-left-color:#0A84FF;}"
        ".badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:0.75rem;margin-left:8px;background:#333;}"
        ".url{font-family:ui-monospace,Menlo,monospace;font-size:0.8rem;word-break:break-all;}"
        "details{margin-top:12px;}"
        "summary{cursor:pointer;color:#aaa;}"
        ".row{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-start;margin-top:8px;}"
        ".imgbox{flex:1;min-width:200px;}"
        ".imgbox img{max-width:100%;border-radius:4px;height:auto;}"
        ".strip-grid{display:flex;flex-wrap:wrap;gap:16px;margin-top:10px;align-items:flex-start;}"
        ".strip-cell{flex:0 1 320px;min-width:220px;max-width:100%;}"
        ".strip-cell img{max-width:100%;border-radius:4px;height:auto;display:block;}"
        ".muted{color:#888;font-size:0.85rem;}"
        "</style></head><body>"
    )
    parts.append("<header><h1>PixelMatcher</h1>")
    parts.append(f"<div class='meta'>Sitemap: {html_module.escape(sitemap_url)}</div>")
    parts.append(f"<div class='meta'>Run: {html_module.escape(ts)}")
    if aborted:
        parts.append(" <strong>(aborted)</strong>")
    parts.append("</div>")
    parts.append(
        "<div class='chips'>"
        f"<span>Total: {total}</span>"
        f"<span>✓ Passed: {passed}</span>"
        f"<span>✗ Failed: {failed}</span>"
        f"<span>⚠ Errors: {errors}</span>"
        f"<span>★ New: {new_pages}</span>"
        "</div></header><div class='wrap'>"
    )

    for o in sorted_pages:
        cls = "card"
        if o.status == "FAIL":
            cls += " fail"
        elif o.status == "PASS":
            cls += " pass"
        elif o.status == "ERROR":
            cls += " err"
        elif o.status == "NEW":
            cls += " new"

        parts.append(f"<section class='{cls}'>")
        parts.append(f"<div><span class='url'>{html_module.escape(o.url)}</span>")
        parts.append(f"<span class='badge'>{html_module.escape(o.status)}</span></div>")

        if o.status == "ERROR":
            parts.append(f"<p class='muted'>{html_module.escape(o.error or 'Unknown error')}</p>")
            parts.append("</section>")
            continue

        summary_txt = f"{o.strip_count} strips"
        if o.status != "NEW":
            summary_txt += f" | {o.changed_count} changed | {o.max_pct * 100:.1f}% max diff"
        parts.append(f"<p class='muted'>{html_module.escape(summary_txt)}</p>")

        if o.status == "NEW":
            parts.append("<p class='muted'>New page — added to baseline</p>")
            new_paths = sorted_strip_paths(run_screenshots_dir, o.slug)
            if not new_paths:
                parts.append("<p class='muted'>No strip images captured</p></section>")
                continue
            parts.append("<details open><summary>All strip screenshots (current run)</summary>")
            parts.append("<div class='strip-grid'>")
            for p in new_paths:
                idx = strip_index_from_name(p)
                b64 = image_to_report_b64(p, cfg)
                if not b64:
                    continue
                lab = strip_scroll_label(idx, cfg.strip_height)
                parts.append(
                    f"<div class='strip-cell'><div class='muted'>{html_module.escape(lab)}</div>"
                    f"<img loading='lazy' src='data:image/jpeg;base64,{b64}' alt=''></div>"
                )
            parts.append("</div></details></section>")
            continue

        if o.status == "PASS":
            parts.append("<details><summary>View screenshot (strip 0)</summary>")
            strip0_paths = sorted_strip_paths(run_screenshots_dir, o.slug)
            if strip0_paths:
                p0 = strip0_paths[0]
                b64 = image_to_report_b64(p0, cfg)
                if b64:
                    parts.append(
                        f"<div style='margin-top:8px'>"
                        f"<img loading='lazy' src='data:image/jpeg;base64,{b64}' "
                        f"style='max-width:400px;border-radius:4px;' alt=''></div>"
                    )
                else:
                    parts.append("<p class='muted'>No strip image</p>")
            else:
                parts.append("<p class='muted'>No strip images</p>")
            parts.append("</details></section>")
            continue

        if o.status == "FAIL":
            failed_strips = [s for s in o.strips if not s.passed]
            passed_strips = [s for s in o.strips if s.passed]
            for s in failed_strips:
                parts.append(f"<p class='muted'>{html_module.escape(s.label)}</p><div class='row'>")
                if s.kind == "REMOVED":
                    b64b = image_to_report_b64(s.baseline_path, cfg)
                    parts.append(
                        f"<div class='imgbox'><div class='muted'>Baseline</div>"
                        f"<img loading='lazy' src='data:image/jpeg;base64,{b64b}' alt=''></div>"
                    )
                    parts.append("<div class='imgbox'><div class='muted'>Current</div><p class='muted'>(removed)</p></div>")
                    parts.append("<div class='imgbox'><div class='muted'>Diff</div><p class='muted'>—</p></div>")
                elif s.kind == "NEW_SECTION":
                    b64c = image_to_report_b64(s.current_path, cfg)
                    parts.append("<div class='imgbox'><div class='muted'>Baseline</div><p class='muted'>—</p></div>")
                    parts.append(
                        f"<div class='imgbox'><div class='muted'>Current</div>"
                        f"<img loading='lazy' src='data:image/jpeg;base64,{b64c}' alt=''></div>"
                    )
                    parts.append("<div class='imgbox'><div class='muted'>Diff</div><p class='muted'>New section</p></div>")
                else:
                    b64b = image_to_report_b64(s.baseline_path, cfg)
                    b64c = image_to_report_b64(s.current_path, cfg)
                    b64d = s.diff_b64
                    parts.append(
                        f"<div class='imgbox'><div class='muted'>Baseline</div>"
                        f"<img loading='lazy' src='data:image/jpeg;base64,{b64b}' alt=''></div>"
                    )
                    parts.append(
                        f"<div class='imgbox'><div class='muted'>Current</div>"
                        f"<img loading='lazy' src='data:image/jpeg;base64,{b64c}' alt=''></div>"
                    )
                    parts.append(
                        f"<div class='imgbox'><div class='muted'>Diff</div>"
                        f"<img loading='lazy' src='data:image/jpeg;base64,{b64d}' alt=''></div>"
                    )
                parts.append("</div>")

            if passed_strips:
                n = len(passed_strips)
                parts.append(f"<details open><summary>{n} section(s) unchanged (thumbnails)</summary>")
                parts.append("<div class='strip-grid'>")
                for s in passed_strips:
                    cp = s.current_path
                    b64 = image_to_report_b64(cp if cp and cp.exists() else None, cfg)
                    lab = s.label or strip_scroll_label(s.index, cfg.strip_height)
                    if b64:
                        parts.append(
                            f"<div class='strip-cell'><div class='muted'>{html_module.escape(lab)} — "
                            f"{s.pct * 100:.2f}% diff</div>"
                            f"<img loading='lazy' src='data:image/jpeg;base64,{b64}' alt=''></div>"
                        )
                    else:
                        parts.append(f"<p class='muted'>{html_module.escape(lab)} — ok ({s.pct * 100:.2f}%)</p>")
                parts.append("</div></details>")
            parts.append("</section>")
            continue

        parts.append("</section>")

    parts.append("</div></body></html>")
    return "".join(parts)


def count_ok_pages(capture_results: list[dict[str, Any]]) -> int:
    return sum(1 for r in capture_results if r["status"] == "OK")


def ensure_playwright_chromium() -> None:
    """Exit with a clear message if browser binaries were never installed for this Python."""
    if sync_playwright is None:
        return
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            browser.close()
    except Exception as e:
        err = str(e)
        if "Executable doesn't exist" in err or "BrowserType.launch" in err:
            py = sys.executable
            print(
                "\n[PixelMatcher] Chromium is not installed for this Python environment.\n"
                f"  Run:\n    {py} -m playwright install chromium\n"
                "  (Use the same interpreter you use to run pixelmatcher.py.)\n",
                file=sys.stderr,
            )
            sys.exit(1)
        raise


def main() -> None:
    global _compare_mode_active

    parser = argparse.ArgumentParser(description="PixelMatcher — sitemap visual regression testing")
    parser.add_argument("--sitemap", required=True, help="URL to sitemap.xml")
    parser.add_argument(
        "--workers",
        type=int,
        default=5,
        help="Parallel browser instances (default: 5)",
    )
    parser.add_argument("--threshold", type=float, default=0.02, help="Max fraction of differing pixels (default: 0.02)")
    parser.add_argument("--reset", action="store_true", help="Delete baseline and capture a new one")
    parser.add_argument(
        "--viewport-width",
        type=int,
        default=1440,
        metavar="PX",
        help="Browser viewport width in CSS pixels (default: 1440)",
    )
    parser.add_argument(
        "--strip-height",
        type=int,
        default=900,
        metavar="PX",
        help="Height of each vertical strip in CSS pixels / viewport height (default: 900)",
    )
    parser.add_argument(
        "--max-capture-width",
        type=int,
        default=1440,
        metavar="PX",
        help="Max width when saving strip JPEGs after capture (default: 1440 = full viewport, no downscale)",
    )
    parser.add_argument(
        "--jpeg-quality",
        type=int,
        default=85,
        metavar="1-100",
        help="JPEG quality for stored strips and diffs (default: 85)",
    )
    parser.add_argument(
        "--device-scale-factor",
        type=float,
        default=1.0,
        help="Playwright device scale factor (default: 1.0; try 2 on retina for sharper layout)",
    )
    parser.add_argument(
        "--full-page-max-css-height",
        type=int,
        default=40000,
        metavar="PX",
        help="Above this page height, use scroll fallback instead of one full-page bitmap (default: 40000)",
    )
    parser.add_argument(
        "--diff-pixel-threshold",
        type=float,
        default=15.0,
        metavar="CHANNEL_MAX",
        help="Per-channel max diff above which a pixel counts as changed (default: 15)",
    )
    parser.add_argument(
        "--diff-blur",
        type=float,
        default=0.0,
        metavar="RADIUS",
        help="Gaussian blur radius applied to both images before pixel compare (reduces JPEG noise; default: 0)",
    )
    parser.add_argument(
        "--report-thumb-width",
        type=int,
        default=800,
        metavar="PX",
        help="Max width for images embedded in HTML report (default: 800)",
    )
    parser.add_argument(
        "--report-jpeg-quality",
        type=int,
        default=80,
        metavar="1-100",
        help="JPEG quality for embedded report images (default: 80)",
    )
    args = parser.parse_args()

    if not (1 <= args.jpeg_quality <= 100):
        print("[PixelMatcher] --jpeg-quality must be 1–100", file=sys.stderr)
        sys.exit(1)
    if not (1 <= args.report_jpeg_quality <= 100):
        print("[PixelMatcher] --report-jpeg-quality must be 1–100", file=sys.stderr)
        sys.exit(1)
    if args.viewport_width < 320 or args.strip_height < 200:
        print("[PixelMatcher] viewport/strip dimensions too small", file=sys.stderr)
        sys.exit(1)

    cfg = PatrolConfig(
        viewport_width=args.viewport_width,
        strip_height=args.strip_height,
        max_width_capture=args.max_capture_width,
        jpeg_quality=args.jpeg_quality,
        full_page_max_css_height=args.full_page_max_css_height,
        device_scale_factor=args.device_scale_factor,
        diff_pixel_threshold=args.diff_pixel_threshold,
        diff_blur_radius=args.diff_blur,
        report_jpeg_quality=args.report_jpeg_quality,
        report_thumb_max_width=args.report_thumb_width,
    )

    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, _signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _signal_handler)

    sitemap_url = args.sitemap.strip()
    print("[PixelMatcher] Fetching sitemap...", end=" ", flush=True)
    try:
        urls = parse_sitemap(sitemap_url)
    except Exception as e:
        print(f"\n[PixelMatcher] Failed to fetch or parse sitemap: {e}", file=sys.stderr)
        sys.exit(1)
    n = len(urls)
    print(f"{n} URLs found.")

    if n == 0:
        print(
            "[PixelMatcher] No page URLs found. Check the sitemap URL, "
            "XML namespaces (http://www.sitemaps.org/schemas/sitemap/0.9), and index children.",
            file=sys.stderr,
        )
        sys.exit(1)

    slug = sitemap_slug_from_url(sitemap_url)
    root = Path("pixelmatcher") / slug
    baseline_dir = root / "baseline"
    reports_dir = root / "reports"

    if args.reset and baseline_dir.is_dir():
        shutil.rmtree(baseline_dir)

    compare_mode = baseline_has_files(baseline_dir)
    if compare_mode:
        print("[PixelMatcher] Mode: COMPARE (baseline exists)")
    else:
        print("[PixelMatcher] Mode: BASELINE (no baseline yet)")

    run_ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_root = root / "runs" / f"run_{run_ts}"
    screenshots_dir = run_root / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    _compare_mode_active = compare_mode
    try:
        ensure_playwright_chromium()
        print(
            f"[PixelMatcher] Capture: {cfg.viewport_width}×{cfg.strip_height}px, "
            f"strip JPEG max {cfg.max_width_capture}px q={cfg.jpeg_quality}, "
            f"diff channel-max>{cfg.diff_pixel_threshold}",
            flush=True,
        )
        print(f"[PixelMatcher] Screenshotting {n} pages with {args.workers} workers...")
        capture_results = run_screenshots(urls, screenshots_dir, args.workers, cfg)
        print(f"[PixelMatcher] Progress: {n}/{n} done.")

        aborted = _shutdown.is_set()

        if not compare_mode:
            if baseline_dir.exists():
                shutil.rmtree(baseline_dir)
            baseline_dir.mkdir(parents=True, exist_ok=True)
            promote_run_to_baseline(screenshots_dir, baseline_dir)
            pages_ok = count_ok_pages(capture_results)
            strip_n = len(list(baseline_dir.glob("*.jpg")))
            print(
                f"[PixelMatcher] Baseline captured — {pages_ok} pages, {strip_n} strips saved.",
                flush=True,
            )
            print("[PixelMatcher] Run again to compare.", flush=True)
            return

        reports_dir.mkdir(parents=True, exist_ok=True)

        print("[PixelMatcher] Diffing pages...", flush=True)
        outcomes = run_compare(baseline_dir, screenshots_dir, capture_results, args.threshold, cfg)
        merge_new_baselines(baseline_dir, screenshots_dir, outcomes)

        passed = sum(1 for o in outcomes if o.status == "PASS")
        failed = sum(1 for o in outcomes if o.status == "FAIL")
        errors = sum(1 for o in outcomes if o.status == "ERROR")
        new_pages = sum(1 for o in outcomes if o.status == "NEW")
        print(
            f"[PixelMatcher] ✓ {passed} passed  ✗ {failed} failed  ⚠ {errors} errors  ★ {new_pages} new",
            flush=True,
        )

        html = build_html_report(sitemap_url, run_ts, outcomes, screenshots_dir, aborted, cfg)
        report_path = reports_dir / f"report_{run_ts}.html"
        report_path.write_text(html, encoding="utf-8")
        print(f"[PixelMatcher] Report → {report_path}", flush=True)
    finally:
        _compare_mode_active = False


if __name__ == "__main__":
    main()
