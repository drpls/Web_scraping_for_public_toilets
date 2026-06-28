"""Viability test: extract 6 reviews from a known Venice restroom.

This script verifies that the extraction approach works before building
the full pipeline.

Success criteria:
- >= 6 reviews extracted
- Each review has: author, rating (1-5), text, date
- No crashes or timeouts
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import structlog

from config import ExtractionConfig
from extractor.auth import BrowserManager, handle_cookie_consent, dismiss_sign_in_prompt
from extractor.anti_detect import scroll_review_panel
from models import Review
from gmaps_selectors import PlaceSelectors as Place

logger = structlog.get_logger(__name__)

TARGET_REVIEWS = 6

# Google's relative date strings (English UI). Values are days per unit.
# These are approximations — Google itself only renders this granularity.
_REL_UNITS = {
    "minute": 1 / 1440,
    "hour": 1 / 24,
    "day": 1,
    "week": 7,
    "month": 30,
    "year": 365,
}


def parse_relative_date(text: str, now: datetime | None = None) -> datetime | None:
    """Parse 'a week ago', '3 months ago', etc. into an absolute datetime."""
    if not text:
        return None
    now = now or datetime.now()
    s = text.strip().lower()
    m = re.match(r"(?:a|an|(\d+))\s+(minute|hour|day|week|month|year)s?\s+ago", s)
    if not m:
        return None
    qty = int(m.group(1)) if m.group(1) else 1
    days = _REL_UNITS[m.group(2)] * qty
    return now - timedelta(days=days)


# Matches Maps' place identifier in the URL: !1s0x<hex>:0x<hex>
_PLACE_ID_RE = re.compile(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)")


def extract_place_id_from_url(url: str) -> str | None:
    m = _PLACE_ID_RE.search(url)
    return m.group(1) if m else None

# Google Maps search URL for "bagno pubblico Venezia"
# We'll search for this and pick the first result.
# hl=en forces English UI so our selectors match (no Italian "Recensioni").
TEST_URL = (
    "https://www.google.com/maps/search/bagno+pubblico+Venezia/"
    "@45.4408,12.3155,14z?hl=en"
)
TEST_OUTPUT = Path("data/test_output.json")
DEBUG_SHOT = Path("data/test_debug.png")


async def run_viability_test() -> bool:
    """Run the viability test.

    Returns True if test passes (>= 6 reviews extracted).
    """
    config = ExtractionConfig(
        headless=False,
        max_reviews_per_place=TARGET_REVIEWS + 2,
        scroll_delay_ms=2000,
    )

    start_time = time.time()
    reviews: list[Review] = []

    async with BrowserManager(config) as context:
        page = await context.new_page()

        logger.info("navigating_to_search", url=TEST_URL)
        # domcontentloaded, not networkidle: Google Maps keeps streaming tile
        # and telemetry requests, so networkidle never fires.
        await page.goto(TEST_URL, wait_until="domcontentloaded", timeout=30000)

        # Cookie consent may live on consent.google.com and redirect back.
        await handle_cookie_consent(page)
        try:
            await page.wait_for_url("**/maps/**", timeout=15000)
        except Exception:
            logger.warning("did_not_reach_maps_url", current=page.url)

        # Wait for the results feed to actually render rather than a fixed sleep.
        try:
            await page.wait_for_selector(
                'div[role="feed"] a.hfpxzc, a.hfpxzc, div.Nv2PK a',
                timeout=20000,
            )
        except Exception:
            logger.error("results_feed_never_rendered", url=page.url)
            await page.screenshot(path=str(DEBUG_SHOT), full_page=True)
            logger.info("debug_screenshot_saved", path=str(DEBUG_SHOT))
            await page.close()
            return False

        await dismiss_sign_in_prompt(page)

        # Click the first search result. Use a Locator so Playwright auto-scrolls
        # the (virtualized) feed item into view before clicking.
        first_result = page.locator('a.hfpxzc').first
        try:
            await first_result.wait_for(state="visible", timeout=10000)
        except Exception:
            logger.error("no_search_results_found")
            await page.screenshot(path=str(DEBUG_SHOT), full_page=True)
            await page.close()
            return False

        logger.info("clicking_first_result")
        await first_result.scroll_into_view_if_needed()
        await first_result.click()

        # Maps puts the place id in the URL when the panel opens.
        # This is the only reliable signal that navigation actually happened.
        try:
            await page.wait_for_url("**/maps/place/**", timeout=15000)
        except Exception:
            logger.error("place_url_never_appeared", current=page.url)
            await page.screenshot(path=str(DEBUG_SHOT), full_page=True)
            await page.close()
            return False

        # Now wait for the *place* h1 specifically (h1.DUwDvf), not a generic h1.
        try:
            await page.wait_for_selector("h1.DUwDvf", timeout=15000)
        except Exception:
            logger.warning("place_panel_header_not_found")

        # Capture the place name and Google place id from the now-loaded panel.
        place_name = "unknown"
        try:
            h1 = await page.query_selector("h1.DUwDvf")
            if h1:
                place_name = (await h1.inner_text()).strip()
        except Exception:
            pass
        place_id = extract_place_id_from_url(page.url) or "unknown"
        logger.info("place_identified", name=place_name, place_id=place_id)

        await handle_cookie_consent(page)
        await dismiss_sign_in_prompt(page)

        # Open the Reviews tab. role="tab" avoids matching "Write a review" or
        # the generic review-count badge. Try ordered most → least specific.
        review_selectors = [
            'button[role="tab"][aria-label*="Reviews"]',
            'button[role="tab"][aria-label*="review" i]',
            'button[jsaction*="pane.rating.moreReviews"]',
            'button[aria-label*="reviews for" i]',
        ]

        reviews_opened = False
        for sel in review_selectors:
            try:
                el = await page.query_selector(sel)
                if el:
                    await el.click()
                    reviews_opened = True
                    logger.info("reviews_tab_opened", selector=sel)
                    break
            except Exception:
                continue

        if not reviews_opened:
            logger.error("could_not_open_reviews_tab")
            await page.screenshot(path=str(DEBUG_SHOT), full_page=True)
            logger.info("debug_screenshot_saved", path=str(DEBUG_SHOT))
            await page.close()
            return False

        # Wait for at least one review card to render.
        try:
            await page.wait_for_selector(
                'div.jftiEf, div[data-review-id]', timeout=15000
            )
        except Exception:
            logger.warning("no_review_cards_after_tab_click")

        # Try to sort by newest
        try:
            sort_btn = await page.query_selector(
                'button[aria-label*="Sort"], div.Mjt6Ue button'
            )
            if sort_btn:
                await sort_btn.click()
                await asyncio.sleep(1)
                newest = await page.query_selector(
                    'menuitem[data-value="1"], div[role="option"]:has-text("Newest")'
                )
                if newest:
                    await newest.click()
                    await asyncio.sleep(2)
                    logger.info("sorted_reviews_by_newest")
        except Exception as e:
            logger.debug("sort_failed", error=str(e))

        # Scroll and extract reviews
        no_new_count = 0
        seen_ids = set()

        while len(reviews) < TARGET_REVIEWS and no_new_count < 5:
            # Scroll the review panel
            count = await scroll_review_panel(page, config)

            # Extract reviews from DOM
            raw_reviews = await page.evaluate("""
                () => {
                    const reviews = [];
                    const containers = document.querySelectorAll(
                        'div.jftiEf, div[data-review-id]'
                    );
                    for (const container of containers) {
                        try {
                            const authorEl = container.querySelector(
                                'div.d4r55, .WNxzHc a'
                            );
                            const author = authorEl
                                ? authorEl.textContent.trim()
                                : 'Unknown';

                            const starsEl = container.querySelector(
                                'span.kvMYJc, span[role="img"][aria-label*="star"]'
                            );
                            let rating = 0;
                            if (starsEl) {
                                const label = starsEl.getAttribute('aria-label') || '';
                                const m = label.match(/(\\d)/);
                                if (m) rating = parseInt(m[1]);
                            }

                            const textEl = container.querySelector('span.wiI7pd');
                            const text = textEl ? textEl.textContent.trim() : '';

                            const dateEl = container.querySelector('span.rsqaWe');
                            const dateStr = dateEl
                                ? dateEl.textContent.trim()
                                : '';

                            const reviewId =
                                container.getAttribute('data-review-id') || '';

                            if (author !== 'Unknown' || text) {
                                reviews.push({
                                    author,
                                    rating,
                                    text,
                                    dateStr,
                                    reviewId,
                                });
                            }
                        } catch (e) {}
                    }
                    return reviews;
                }
            """)

            new_found = 0
            for raw in raw_reviews:
                rid = raw.get("reviewId") or f"{raw['author']}_{raw['dateStr']}"
                if rid in seen_ids:
                    continue
                seen_ids.add(rid)

                # Google Maps shows relative dates ("3 months ago"), not absolute.
                published_at = parse_relative_date(raw["dateStr"])

                review = Review(
                    review_id=rid,
                    place_id=place_id,
                    author_name=raw["author"],
                    rating=raw["rating"],
                    text=raw["text"],
                    published_at=published_at,
                )
                reviews.append(review)
                new_found += 1

            if new_found == 0:
                no_new_count += 1
            else:
                no_new_count = 0

            logger.info(
                "scroll_progress",
                collected=len(reviews),
                target=TARGET_REVIEWS,
            )

        await page.close()

    # Trim to target
    reviews = reviews[:TARGET_REVIEWS]

    elapsed = time.time() - start_time

    # Save results
    TEST_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    output_data = {
        "test_passed": len(reviews) >= TARGET_REVIEWS,
        "place_name": place_name,
        "place_id": place_id,
        "reviews_count": len(reviews),
        "elapsed_seconds": round(elapsed, 2),
        "reviews": [r.model_dump(mode="json") for r in reviews],
    }
    TEST_OUTPUT.write_text(json.dumps(output_data, indent=2, default=str))

    # Print summary
    print("\n" + "=" * 60)
    print("VIABILITY TEST RESULTS")
    print("=" * 60)
    print(f"Place: {place_name} ({place_id})")
    print(f"Reviews extracted: {len(reviews)} / {TARGET_REVIEWS}")
    print(f"Elapsed time: {elapsed:.1f}s")
    print(f"Test passed: {output_data['test_passed']}")

    if reviews:
        print("\nSample reviews:")
        for i, r in enumerate(reviews[:3], 1):
            print(f"  {i}. [{r.rating}★] {r.author_name}: {r.text[:80]}...")

    print(f"\nFull output saved to: {TEST_OUTPUT}")
    print("=" * 60)

    return output_data["test_passed"]


if __name__ == "__main__":
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )
    success = asyncio.run(run_viability_test())
    exit(0 if success else 1)