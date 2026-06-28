"""Review scraper: extracts every review from a Google Maps place page.

The flow (proven by test_viability):
  goto place URL (with hl=en) → wait for /maps/place/ in URL → wait for h1.DUwDvf
  → click Reviews tab (role=tab) → sort newest → scroll until end-of-list signal
  → extract via JS evaluate.
"""

from __future__ import annotations

import asyncio
import hashlib
import re

import structlog
from playwright.async_api import BrowserContext, Page

from config import CityConfig, ExtractionConfig
from extractor.anti_detect import (
    detect_captcha,
    random_delay,
    scroll_review_panel,
)
from extractor.auth import (
    check_eea_auth_wall,
    dismiss_sign_in_prompt,
    handle_cookie_consent,
)
from extractor.utils import (
    extract_place_id_from_url,
    force_english,
    parse_relative_date,
)
from gmaps_selectors import PlaceSelectors as Place
from models import PublicRestroom, Review

logger = structlog.get_logger(__name__)


class CaptchaDetected(Exception):
    """Raised so the pipeline can trigger backoff."""


def _make_review_id(place_id: str, author: str, date: str) -> str:
    return hashlib.md5(f"{place_id}|{author}|{date}".encode()).hexdigest()


async def _open_reviews_tab(page: Page) -> bool:
    """Click the Reviews tab. role=tab avoids matching 'Write a review'."""
    selectors = [
        'button[role="tab"][aria-label*="Reviews"]',
        'button[role="tab"][aria-label*="review" i]',
        'button[jsaction*="pane.rating.moreReviews"]',
        'button[aria-label*="reviews for" i]',
    ]
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                await el.click()
                logger.debug("reviews_tab_opened", selector=sel)
                return True
        except Exception:
            continue
    return False


async def _sort_reviews_newest(page: Page) -> bool:
    try:
        sort_btn = await page.query_selector(
            'button[aria-label*="Sort"], div.Mjt6Ue button'
        )
        if not sort_btn:
            return False
        await sort_btn.click()
        await asyncio.sleep(1)
        newest = await page.query_selector(
            'div[role="menuitemradio"]:has-text("Newest"), '
            'div[role="option"]:has-text("Newest"), '
            'menuitem[data-value="1"]'
        )
        if newest:
            await newest.click()
            await asyncio.sleep(1.5)
            logger.debug("reviews_sorted_newest")
            return True
    except Exception as e:
        logger.debug("sort_failed", error=str(e))
    return False


_EXTRACT_REVIEWS_JS = """
() => {
    const reviews = [];
    document.querySelectorAll('div.jftiEf, div[data-review-id]').forEach(c => {
        try {
            const authorEl = c.querySelector('div.d4r55, .WNxzHc a, .rsqaWe');
            const author = authorEl ? authorEl.textContent.trim() : 'Unknown';

            const starsEl = c.querySelector(
                'span.kvMYJc, span[role="img"][aria-label*="star" i]'
            );
            let rating = 0;
            if (starsEl) {
                const a = starsEl.getAttribute('aria-label') || '';
                const m = a.match(/(\\d)/);
                if (m) rating = parseInt(m[1]);
            }

            // Click "More" if the text is collapsed — Maps truncates long reviews.
            const more = c.querySelector('button.w8nwRe');
            if (more) try { more.click(); } catch (e) {}

            const textEl = c.querySelector('span.wiI7pd, .wiI7pd');
            const text = textEl ? textEl.textContent.trim() : '';

            const dateEl = c.querySelector('span.rsqaWe');
            const dateStr = dateEl ? dateEl.textContent.trim() : '';

            const reviewId = c.getAttribute('data-review-id') || '';

            if (author !== 'Unknown' || text) {
                reviews.push({ author, rating, text, dateStr, reviewId });
            }
        } catch (e) {}
    });
    return reviews;
}
"""


async def _extract_reviews_from_dom(page: Page) -> list[dict]:
    return await page.evaluate(_EXTRACT_REVIEWS_JS)


async def scrape_reviews_for_place(
    place: PublicRestroom,
    context: BrowserContext,
    config: ExtractionConfig,
    city: CityConfig | None = None,
) -> list[Review]:
    """Scrape every review for one place (up to the safety cap).

    Args:
        place: The restroom to scrape reviews for.
        context: Playwright browser context.
        config: Extraction configuration.
        city: Optional city config for logging context.

    Returns:
        List of extracted Review objects.
    """
    if not place.google_maps_url:
        logger.warning("no_google_maps_url", name=place.name)
        return []

    city_name = city.name if city else place.city
    page = await context.new_page()
    reviews: list[Review] = []
    seen_ids: set[str] = set()

    try:
        url = force_english(place.google_maps_url)
        logger.info(
            "navigating_to_place",
            name=place.name,
            city=city_name,
            place_id=place.place_id,
        )
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        await handle_cookie_consent(page)
        await dismiss_sign_in_prompt(page)

        # Confirm we actually landed on a place page (URL signal is reliable).
        try:
            await page.wait_for_url("**/maps/place/**", timeout=15000)
        except Exception:
            if await detect_captcha(page):
                raise CaptchaDetected(f"captcha on place {place.name}")
            logger.warning("place_url_never_appeared", name=place.name, url=page.url)
            return reviews

        # Refine place_id from the resolved URL (more reliable than the search href).
        better_id = extract_place_id_from_url(page.url)
        if better_id and better_id != place.place_id:
            logger.debug(
                "place_id_refined", old=place.place_id, new=better_id, name=place.name
            )
            place.place_id = better_id

        try:
            await page.wait_for_selector("h1.DUwDvf", timeout=15000)
        except Exception:
            logger.warning("place_header_missing", name=place.name)

        if await check_eea_auth_wall(page):
            logger.warning("eea_wall_blocked_reviews", name=place.name)
            # Still try metadata extraction below.

        await _extract_place_metadata(page, place)

        if not await _open_reviews_tab(page):
            logger.warning("reviews_tab_failed", name=place.name)
            return reviews

        try:
            await page.wait_for_selector(
                'div.jftiEf, div[data-review-id]', timeout=15000
            )
        except Exception:
            logger.warning("no_review_cards", name=place.name)
            return reviews

        await _sort_reviews_newest(page)

        target = config.max_reviews_per_place
        no_new_limit = config.no_new_review_scrolls_max
        no_new_count = 0

        # Loop until either Google stops giving us new reviews (real
        # end-of-list signal) or we hit the safety cap.
        while len(reviews) < target and no_new_count < no_new_limit:
            await scroll_review_panel(page, config)
            raw_reviews = await _extract_reviews_from_dom(page)

            new_this_round = 0
            for raw in raw_reviews:
                review_id = raw.get("reviewId") or _make_review_id(
                    place.place_id, raw["author"], raw["dateStr"]
                )
                if review_id in seen_ids:
                    continue
                seen_ids.add(review_id)

                review = Review(
                    review_id=review_id,
                    place_id=place.place_id,
                    author_name=raw["author"],
                    rating=raw["rating"] or 1,  # avoid validator reject on 0
                    text=raw["text"],
                    published_at=parse_relative_date(raw.get("dateStr", "")),
                )
                reviews.append(review)
                new_this_round += 1

            if new_this_round == 0:
                no_new_count += 1
            else:
                no_new_count = 0

            logger.debug(
                "review_scroll_progress",
                name=place.name,
                city=city_name,
                collected=len(reviews),
                empty_scrolls=no_new_count,
            )

        if await detect_captcha(page):
            raise CaptchaDetected(f"captcha during reviews of {place.name}")

        logger.info(
            "reviews_scraped",
            name=place.name,
            city=city_name,
            count=len(reviews),
            place_id=place.place_id,
        )

    except CaptchaDetected:
        raise
    except Exception as e:
        logger.error("review_scraping_error", name=place.name, error=str(e))
    finally:
        await page.close()

    return reviews


async def _extract_place_metadata(page: Page, place: PublicRestroom) -> None:
    """Pull rating / review count / address / phone / website off the place page."""
    try:
        rating_el = await page.query_selector(Place.PLACE_RATING)
        if rating_el:
            text = (await rating_el.text_content() or "").strip()
            try:
                place.rating = float(text.replace(",", "."))
            except ValueError:
                pass

        count_el = await page.query_selector(Place.PLACE_REVIEW_COUNT)
        if count_el:
            text = (await count_el.text_content() or "")
            m = re.search(r"[\d.]+", text.replace(".", "").replace(",", ""))
            if m:
                try:
                    place.user_ratings_total = int(m.group(0))
                except ValueError:
                    pass

        addr_el = await page.query_selector(Place.PLACE_ADDRESS)
        if addr_el:
            place.address = (await addr_el.text_content()) or place.address

        phone_el = await page.query_selector(Place.PLACE_PHONE)
        if phone_el:
            place.phone_number = (
                await phone_el.text_content()
            ) or place.phone_number

        web_el = await page.query_selector(Place.PLACE_WEBSITE)
        if web_el:
            place.website = (
                await web_el.get_attribute("href")
            ) or place.website
    except Exception as e:
        logger.debug("metadata_extraction_error", error=str(e))


async def scrape_all_reviews(
    restrooms: list[PublicRestroom],
    context: BrowserContext,
    config: ExtractionConfig,
    city: CityConfig | None = None,
) -> dict[str, list[Review]]:
    """Per-place review scrape. Kept for API compatibility — the pipeline now
    drives per-place scraping directly so it can rate-limit and recycle.
    """
    all_reviews: dict[str, list[Review]] = {}
    total = len(restrooms)
    for i, place in enumerate(restrooms):
        logger.info(
            "scraping_place_reviews",
            name=place.name,
            progress=f"{i + 1}/{total}",
        )
        all_reviews[place.place_id] = await scrape_reviews_for_place(
            place, context, config, city
        )
        if i < total - 1:
            await random_delay(config)
    return all_reviews
