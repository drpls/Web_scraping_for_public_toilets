"""Photo scraper: downloads place photos from Google Maps.

Two-level strategy for maximum reliability:
  Level 1 (preferred) — network interception: listen for image responses from
    googleusercontent.com as the photo gallery loads.
  Level 2 (fallback) — DOM extraction: extract <img> src attributes and download
    via httpx.

The scraper clicks the Photos tab on the place page, scrolls the gallery to
trigger lazy-loading, then saves captured images to disk.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import re
from pathlib import Path

from PIL import Image

import httpx
import structlog
from playwright.async_api import BrowserContext, Page, Response
from slugify import slugify

from config import CityConfig, ExtractionConfig
from extractor.anti_detect import detect_captcha, random_delay
from extractor.auth import dismiss_sign_in_prompt, handle_cookie_consent
from extractor.utils import force_english
from gmaps_selectors import PhotoSelectors as Photo
from models import PublicRestroom

logger = structlog.get_logger(__name__)


def _make_photo_dir(config: ExtractionConfig, city: CityConfig, place: PublicRestroom) -> Path:
    """Build the photo directory for a place.

    Structure: data/photos/<city_native_slugified>/<place_name_slugified>/

    Args:
        config: Extraction configuration.
        city: City configuration.
        place: The restroom to create a folder for.

    Returns:
        Path to the photo directory (created if missing).
    """
    city_slug = slugify(city.native_name, lowercase=True)
    place_slug = slugify(place.name, lowercase=True) or slugify(place.place_id, lowercase=True)
    # Avoid name collisions: append last 6 hex chars of place_id hash
    short_hash = hashlib.md5(place.place_id.encode()).hexdigest()[:6]
    photo_dir = config.photos_dir / city_slug / f"{place_slug}_{short_hash}"
    photo_dir.mkdir(parents=True, exist_ok=True)
    return photo_dir


def _next_photo_filename(photo_dir: Path) -> str:
    """Return the next sequential filename (photo_001.jpg, photo_002.jpg, ...)."""
    existing = sorted(photo_dir.glob("photo_*.jpg"))
    if not existing:
        return "photo_001.jpg"
    last_num = 0
    for f in existing:
        m = re.search(r"photo_(\d+)", f.stem)
        if m:
            last_num = max(last_num, int(m.group(1)))
    return f"photo_{last_num + 1:03d}.jpg"


def _compress_photo(data: bytes, max_bytes: int) -> bytes:
    """Compress image data to fit within max_bytes.

    Strategy: progressively lower JPEG quality, then downscale if needed.
    Always returns valid JPEG bytes that fit within the budget.

    Args:
        data: Raw image bytes (any format Pillow can read).
        max_bytes: Maximum allowed file size in bytes.

    Returns:
        JPEG-compressed bytes fitting within max_bytes.
    """
    if len(data) <= max_bytes:
        return data

    try:
        img = Image.open(io.BytesIO(data))
    except Exception:
        # Can't decode — return original and let caller handle it
        return data

    img = img.convert("RGB")  # ensure JPEG-compatible mode

    # Try progressively lower quality
    for quality in (85, 70, 55, 40, 30):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= max_bytes:
            return buf.getvalue()

    # Still too large — downscale in steps
    for scale in (0.75, 0.5, 0.35, 0.25):
        new_w = max(1, int(img.width * scale))
        new_h = max(1, int(img.height * scale))
        resized = img.resize((new_w, new_h), Image.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format="JPEG", quality=40, optimize=True)
        if buf.tell() <= max_bytes:
            return buf.getvalue()

    # Absolute fallback — tiny thumbnail
    resized = img.resize((320, 240), Image.LANCZOS)
    buf = io.BytesIO()
    resized.save(buf, format="JPEG", quality=30, optimize=True)
    return buf.getvalue()


async def _click_photos_tab(page: Page) -> bool:
    """Click the Photos tab on the place page.

    Returns True if the tab was successfully clicked.
    """
    selectors = [
        Photo.PHOTOS_TAB,
        Photo.PHOTOS_TAB_ALT,
        Photo.PHOTO_HERO,
    ]
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                await el.click()
                logger.debug("photos_tab_clicked", selector=sel)
                await asyncio.sleep(2.0)
                return True
        except Exception:
            continue
    return False


async def _scroll_photo_gallery(page: Page, rounds: int = 6) -> None:
    """Scroll the photo gallery to trigger lazy loading of more images.

    Args:
        page: Playwright page.
        rounds: Number of scroll iterations.
    """
    import random

    for _ in range(rounds):
        # Use mouse wheel (human-ish) rather than scrollTo
        delta = random.randint(400, 900)
        await page.mouse.wheel(0, delta)
        await asyncio.sleep(random.uniform(0.8, 1.5))


async def scrape_photos_for_place(
    place: PublicRestroom,
    context: BrowserContext,
    config: ExtractionConfig,
    city: CityConfig,
) -> list[str]:
    """Download photos for a single place.

    Opens the place page, clicks Photos, scrolls to load images, then
    intercepts network responses to save image files.

    Args:
        place: The restroom to download photos for.
        context: Playwright browser context.
        config: Extraction configuration.
        city: City configuration for folder naming.

    Returns:
        List of saved photo filenames (relative to the place's photo dir).
    """
    if not place.google_maps_url:
        logger.warning("no_url_for_photos", name=place.name)
        return []

    photo_dir = _make_photo_dir(config, city, place)
    saved_filenames: list[str] = []
    captured_urls: set[str] = set()

    # Determine the effective max (0 = unlimited → use a large sentinel)
    effective_max = config.max_photos_per_place if config.max_photos_per_place > 0 else 999

    # --- Level 1: network interception ---
    async def _on_response(response: Response) -> None:
        """Capture image responses from googleusercontent.com."""
        nonlocal saved_filenames
        if len(saved_filenames) >= effective_max:
            return

        url = response.url
        # Only capture actual place photos, not UI sprites/icons
        if response.request.resource_type != "image":
            return
        if "googleusercontent.com" not in url:
            return
        # Skip tiny thumbnails and UI icons — only keep substantial images
        # Google serves photos at various sizes via URL params (=w123-h456)
        # Skip anything explicitly sized under 100px
        if re.search(r"=w(\d+)", url):
            width = int(re.search(r"=w(\d+)", url).group(1))  # type: ignore[union-attr]
            if width < 100:
                return
        if re.search(r"=s(\d+)", url):
            size = int(re.search(r"=s(\d+)", url).group(1))  # type: ignore[union-attr]
            if size < 100:
                return

        # Deduplicate by base URL (strip size params)
        base_url = re.sub(r"=w\d+-h\d+.*$", "", url)
        base_url = re.sub(r"=s\d+.*$", "", base_url)
        if base_url in captured_urls:
            return
        captured_urls.add(base_url)

        try:
            body = await response.body()
            if len(body) < 5000:
                # Likely a small icon, not a real photo
                return
            body = _compress_photo(body, config.max_photo_bytes)
            filename = _next_photo_filename(photo_dir)
            (photo_dir / filename).write_bytes(body)
            saved_filenames.append(filename)
            logger.debug(
                "photo_intercepted",
                filename=filename,
                size_kb=round(len(body) / 1024, 1),
                place=place.name,
            )
        except Exception as e:
            logger.debug("photo_intercept_error", error=str(e))

    page = await context.new_page()

    try:
        # Register the network listener before navigating
        page.on("response", _on_response)

        url = force_english(place.google_maps_url)
        logger.info("navigating_for_photos", name=place.name)
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        await handle_cookie_consent(page)
        await dismiss_sign_in_prompt(page)

        try:
            await page.wait_for_url("**/maps/place/**", timeout=15000)
        except Exception:
            if await detect_captcha(page):
                logger.warning("captcha_during_photos", name=place.name)
                return saved_filenames
            logger.warning("place_url_not_reached_for_photos", name=place.name)
            return saved_filenames

        # Wait for the place panel to render
        try:
            await page.wait_for_selector("h1.DUwDvf", timeout=10000)
        except Exception:
            pass

        # Click Photos tab to open the gallery
        if not await _click_photos_tab(page):
            logger.warning("photos_tab_not_found", name=place.name)
            # Fall through to Level 2 — maybe some images loaded on the main page
        else:
            # Wait for gallery images to start loading
            try:
                await page.wait_for_selector(Photo.PHOTO_IMG, timeout=10000)
            except Exception:
                logger.debug("no_gallery_images_loaded", name=place.name)

        # Scroll gallery to trigger lazy-loading of more photos
        scroll_rounds = 8 if effective_max > 20 else 4
        await _scroll_photo_gallery(page, rounds=scroll_rounds)

        # Give the interceptor a moment to catch remaining in-flight responses
        await asyncio.sleep(2.0)

        # --- Level 2: DOM extraction fallback ---
        # If network interception didn't capture enough, try DOM extraction + httpx
        if len(saved_filenames) < 3:
            logger.debug("trying_dom_photo_extraction", name=place.name)
            dom_urls = await page.evaluate("""
                () => {
                    const urls = new Set();
                    // img elements
                    document.querySelectorAll('img[src*="googleusercontent"]').forEach(img => {
                        const src = img.getAttribute('src');
                        if (src) urls.add(src);
                    });
                    // Background images
                    document.querySelectorAll('div[style*="googleusercontent"]').forEach(div => {
                        const style = div.getAttribute('style') || '';
                        const m = style.match(/url\\("?([^"\\)]+)"?\\)/);
                        if (m) urls.add(m[1]);
                    });
                    return [...urls];
                }
            """)

            # Request higher resolution versions of the images
            upgraded_urls = []
            for u in dom_urls:
                # Remove existing size params and request large version
                clean = re.sub(r"=w\d+-h\d+.*$", "=w1200", u)
                clean = re.sub(r"=s\d+.*$", "=w1200", clean)
                upgraded_urls.append(clean)

            async with httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                    )
                },
            ) as client:
                for img_url in upgraded_urls:
                    if len(saved_filenames) >= effective_max:
                        break
                    # Deduplicate
                    base = re.sub(r"=w\d+.*$", "", img_url)
                    if base in captured_urls:
                        continue
                    captured_urls.add(base)

                    try:
                        resp = await client.get(img_url)
                        if resp.status_code == 200 and len(resp.content) > 5000:
                            compressed = _compress_photo(
                                resp.content, config.max_photo_bytes
                            )
                            filename = _next_photo_filename(photo_dir)
                            (photo_dir / filename).write_bytes(compressed)
                            saved_filenames.append(filename)
                            logger.debug(
                                "photo_downloaded_httpx",
                                filename=filename,
                                size_kb=round(len(compressed) / 1024, 1),
                            )
                    except Exception as e:
                        logger.debug("httpx_photo_error", error=str(e))

                    await asyncio.sleep(config.photo_download_delay_s)

        logger.info(
            "photos_scraped",
            name=place.name,
            count=len(saved_filenames),
            dir=str(photo_dir),
        )

    except Exception as e:
        logger.error("photo_scraping_error", name=place.name, error=str(e))
    finally:
        page.remove_listener("response", _on_response)
        await page.close()

    return saved_filenames
