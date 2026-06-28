"""Search scraper: discovers public restrooms via Google Maps search.

For each query: navigate to Maps centered on the city, type the query, scroll
the results panel to the bottom, then extract each result's stable Maps
place id and surface data.
"""

from __future__ import annotations

import asyncio
import hashlib
import random

import structlog
from playwright.async_api import BrowserContext, Page

from config import CityConfig, ExtractionConfig
from extractor.anti_detect import detect_captcha
from extractor.auth import dismiss_sign_in_prompt, handle_cookie_consent
from extractor.utils import extract_place_id_from_url, force_english
from gmaps_selectors import NavigationSelectors as Nav  # noqa: F401
from gmaps_selectors import SearchSelectors as Search
from models import PublicRestroom

logger = structlog.get_logger(__name__)


class CaptchaDetected(Exception):
    """Raised so the pipeline can trigger backoff instead of continuing blindly."""


async def _dismiss_popups(page: Page) -> None:
    await handle_cookie_consent(page)
    await dismiss_sign_in_prompt(page)
    await asyncio.sleep(0.5)


async def _scroll_results_to_end(page: Page, config: ExtractionConfig) -> int:
    """Scroll the results feed until no new items appear. Returns final count."""
    feed = await page.query_selector(Search.RESULTS_CONTAINER)
    if not feed:
        return 0

    last_count = -1
    stable_rounds = 0
    rounds = 0
    while stable_rounds < 3 and rounds < 40:
        rounds += 1
        await feed.evaluate("el => el.scrollBy(0, el.clientHeight)")
        await asyncio.sleep(random.uniform(1.0, 1.8))
        count = await page.evaluate(
            "() => document.querySelectorAll('a.hfpxzc').length"
        )
        if count == last_count:
            stable_rounds += 1
        else:
            stable_rounds = 0
        last_count = count
    logger.debug("results_feed_scrolled", final_count=last_count, rounds=rounds)
    return last_count


async def search_google_maps(
    query: str,
    context: BrowserContext,
    config: ExtractionConfig,
    city: CityConfig,
) -> list[PublicRestroom]:
    """Run one search query and return the results as PublicRestroom objects.

    Uses direct search URL navigation (like the working viability test)
    instead of typing into the search box, which is more reliable.

    Args:
        query: The search query string.
        context: Playwright browser context.
        config: Extraction configuration.
        city: City configuration providing lat/lon and name.

    Returns:
        List of discovered PublicRestroom objects.
    """
    page = await context.new_page()
    restrooms: list[PublicRestroom] = []

    try:
        # Build a direct search URL — proven reliable by the viability test.
        # Avoid duplicating city name if query already contains it.
        query_lower = query.lower()
        city_names = {city.name.lower(), city.native_name.lower()}
        if any(cn in query_lower for cn in city_names):
            full_query = query
        else:
            full_query = f"{query} {city.native_name}"
        query_encoded = full_query.replace(" ", "+")
        maps_url = force_english(
            f"https://www.google.com/maps/search/{query_encoded}/"
            f"@{city.latitude},{city.longitude},14z"
        )
        logger.info("navigating_to_search", query=full_query, city=city.name, url=maps_url)
        # domcontentloaded, not networkidle — Maps never reaches network idle.
        await page.goto(maps_url, wait_until="domcontentloaded", timeout=30000)

        await _dismiss_popups(page)

        # Wait for the actual results feed to render, not a fixed sleep.
        try:
            await page.wait_for_selector(
                'div[role="feed"] a.hfpxzc, a.hfpxzc',
                timeout=20000,
            )
        except Exception:
            if await detect_captcha(page):
                raise CaptchaDetected("captcha on search results")
            logger.warning("no_search_results", query=query, url=page.url)
            return restrooms

        await _dismiss_popups(page)
        await _scroll_results_to_end(page, config)

        results_data = await page.evaluate(
            """
            () => {
                const out = [];
                const seen = new Set();
                document.querySelectorAll('a.hfpxzc').forEach(link => {
                    const href = link.getAttribute('href') || '';
                    if (!href || seen.has(href)) return;
                    seen.add(href);

                    const card = link.closest('div.Nv2PK') || link;

                    const nameEl = card.querySelector('.qBF1Pd, .fontHeadlineSmall')
                        || link;
                    const name = (nameEl.textContent || '').trim();

                    const ratingEl = card.querySelector('.MW4etd');
                    const rating = ratingEl ? parseFloat(ratingEl.textContent) : null;

                    const reviewCountEl = card.querySelector('.UY7F9');
                    let reviewCount = null;
                    if (reviewCountEl) {
                        const m = reviewCountEl.textContent.match(/\\d+/);
                        if (m) reviewCount = parseInt(m[0]);
                    }

                    const addressEl = card.querySelector(
                        '.W4Efsd .W4Efsd span:last-child, .fontBodyMedium span.DkEaL'
                    );
                    const address = addressEl
                        ? addressEl.textContent.trim()
                        : '';

                    let lat = null, lng = null;
                    const c = href.match(/@([\\d.-]+),([\\d.-]+)/);
                    if (c) { lat = parseFloat(c[1]); lng = parseFloat(c[2]); }

                    if (name && href) {
                        out.push({ name, href, address, rating, reviewCount, lat, lng });
                    }
                });
                return out;
            }
            """
        )

        logger.info(
            "search_results_extracted",
            query=query,
            city=city.name,
            count=len(results_data),
        )

        for item in results_data:
            href: str = item["href"]
            # Real Maps place id, not MD5 of href — stable across runs and queries.
            place_id = extract_place_id_from_url(href) or hashlib.md5(
                href.encode()
            ).hexdigest()

            restrooms.append(
                PublicRestroom(
                    place_id=place_id,
                    name=item["name"],
                    city=city.name,
                    address=item.get("address") or None,
                    latitude=item.get("lat") or city.latitude,
                    longitude=item.get("lng") or city.longitude,
                    rating=item.get("rating"),
                    user_ratings_total=item.get("reviewCount"),
                    google_maps_url=force_english(
                        href if href.startswith("http") else f"https://www.google.com{href}"
                    ),
                    search_query=query,
                )
            )

        if await detect_captcha(page):
            raise CaptchaDetected("captcha after search results")

    except CaptchaDetected:
        raise
    except Exception as e:
        logger.error("search_error", query=query, city=city.name, error=str(e))
    finally:
        await page.close()

    return restrooms


async def discover_restrooms(
    context: BrowserContext,
    config: ExtractionConfig,
    city: CityConfig,
) -> list[PublicRestroom]:
    """Run all configured queries for a city and return deduplicated restrooms.

    Args:
        context: Playwright browser context.
        config: Extraction configuration.
        city: City configuration with search queries, lat/lon.

    Returns:
        Deduplicated list of PublicRestroom objects.
    """
    all_restrooms: dict[str, PublicRestroom] = {}

    for i, query in enumerate(city.search_queries):
        logger.info(
            "running_search_query",
            query=query,
            city=city.name,
            progress=f"{i + 1}/{len(city.search_queries)}",
        )
        try:
            results = await search_google_maps(query, context, config, city)
        except CaptchaDetected:
            # Bubble up so the pipeline can decide to back off / abort.
            raise

        for restroom in results:
            if restroom.place_id not in all_restrooms:
                all_restrooms[restroom.place_id] = restroom
                logger.info(
                    "new_restroom_found",
                    name=restroom.name,
                    place_id=restroom.place_id,
                    city=city.name,
                )

        if i < len(city.search_queries) - 1:
            await asyncio.sleep(config.location_delay_s)

    logger.info(
        "discovery_complete",
        city=city.name,
        total_unique=len(all_restrooms),
        queries_run=len(city.search_queries),
    )
    return list(all_restrooms.values())
