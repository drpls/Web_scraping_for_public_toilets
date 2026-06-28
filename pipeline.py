"""Pipeline orchestrator.

Phases (per city):
  1. Discover restrooms via Maps search.
  2. For each restroom, scrape every review.
  3. For each restroom, download photos.

After all cities:
  4. Export everything to CSV.

The per-place loop is designed for long-running survival:
- skip places already in the DB (resume on restart),
- rate-limit globally via a token bucket (max_places_per_minute),
- close & relaunch the browser every N places to drop fingerprint state,
- on CAPTCHA detection, sleep with exponential backoff and retry the same place.
"""

from __future__ import annotations

from datetime import datetime

import structlog

from config import CityConfig, ExtractionConfig
from extractor.anti_detect import CaptchaBackoff, RateLimiter, random_delay
from extractor.auth import BrowserManager
from extractor.openrouter_filter import filter_restrooms_with_openrouter
from extractor.photo_scraper import scrape_photos_for_place
from extractor.review_scraper import (
    CaptchaDetected as ReviewCaptcha,
    scrape_reviews_for_place,
)
from extractor.search_scraper import (
    CaptchaDetected as SearchCaptcha,
    discover_restrooms,
)
from models import ExtractionStats, PublicRestroom
from storage.csv_exporter import export_to_csv
from storage.sqlite_store import SQLiteStore

logger = structlog.get_logger(__name__)


async def _discover_with_backoff(
    browser_mgr_factory,  # callable returning BrowserManager
    config: ExtractionConfig,
    city: CityConfig,
    backoff: CaptchaBackoff,
) -> list[PublicRestroom]:
    """Run Phase 1 with CAPTCHA backoff."""
    while True:
        async with browser_mgr_factory() as context:
            try:
                return await discover_restrooms(context, config, city)
            except SearchCaptcha as e:
                logger.warning("search_captcha", city=city.name, error=str(e))
        # Browser closed — sleep before retry. Give up if exhausted.
        if not await backoff.trigger():
            logger.error("captcha_backoff_exhausted_in_discovery", city=city.name)
            return []


async def _process_city(
    city: CityConfig,
    config: ExtractionConfig,
    store: SQLiteStore,
    stats: ExtractionStats,
) -> None:
    """Process a single city: discover, filter, scrape reviews, download photos.

    Args:
        city: City to process.
        config: Extraction configuration.
        store: SQLite store for persistence.
        stats: Mutable stats object to update.
    """
    logger.info("city_processing_start", city=city.name)

    backoff = CaptchaBackoff(
        base_seconds=config.captcha_backoff_base_s,
        max_attempts=config.captcha_max_attempts,
    )

    def make_browser_mgr() -> BrowserManager:
        return BrowserManager(config)

    # Phase 1 — discover restrooms.
    logger.info("phase_1_start", city=city.name, description="Discovering restrooms")
    restrooms = await _discover_with_backoff(make_browser_mgr, config, city, backoff)
    city_found = len(restrooms)
    stats.total_restrooms_found += city_found
    logger.info(
        "phase_1_complete",
        city=city.name,
        found=city_found,
    )

    if not restrooms:
        logger.warning("no_restrooms_found", city=city.name)
        return

    # Phase 1.5 — LLM classification (OpenRouter) to filter out false positives.
    logger.info(
        "phase_1_5_start",
        city=city.name,
        description="Filtering with OpenRouter",
        candidates=len(restrooms),
    )
    restrooms = await filter_restrooms_with_openrouter(restrooms)
    logger.info(
        "phase_1_5_complete",
        city=city.name,
        kept=len(restrooms),
        rejected=city_found - len(restrooms),
    )

    # Persist the filtered restrooms
    for restroom in restrooms:
        await store.upsert_restroom(restroom)
    stats.total_restrooms_saved += len(restrooms)

    # Phase 2 — review extraction with rate limiting, resumability, recycling.
    logger.info(
        "phase_2_start", city=city.name, description="Scraping reviews"
    )
    backoff.reset()
    rate_limiter = RateLimiter(rate_per_minute=config.max_places_per_minute)

    # Resume: skip places that already have reviews in the DB.
    already_done = (
        set() if config.force_rescrape else await store.get_place_ids_with_reviews()
    )
    pending = [r for r in restrooms if r.place_id not in already_done]
    logger.info(
        "phase_2_plan",
        city=city.name,
        total=len(restrooms),
        already_done=len(restrooms) - len(pending),
        to_scrape=len(pending),
    )

    total_reviews = 0
    recycle_n = max(1, config.recycle_browser_every_n_places)
    i = 0
    while i < len(pending):
        # Open a fresh browser for each batch — drops accumulated client-side state.
        batch = pending[i : i + recycle_n]
        async with make_browser_mgr() as context:
            for place in batch:
                await rate_limiter.acquire()
                logger.info(
                    "scraping_place_reviews",
                    name=place.name,
                    city=city.name,
                    progress=f"{i + 1}/{len(pending)}",
                )
                try:
                    reviews = await scrape_reviews_for_place(
                        place, context, config, city
                    )
                except ReviewCaptcha as e:
                    logger.warning(
                        "review_captcha",
                        name=place.name,
                        city=city.name,
                        error=str(e),
                    )
                    # Bail out of this batch so we relaunch the browser after sleeping.
                    if not await backoff.trigger():
                        logger.error("captcha_backoff_exhausted_in_reviews")
                        return
                    break  # exits the inner for, closes context, relaunches

                if reviews:
                    await store.upsert_reviews(reviews)
                    total_reviews += len(reviews)
                # Persist any refined place_id / metadata changes.
                await store.upsert_restroom(place)
                backoff.reset()
                i += 1
                await random_delay(config)
            else:
                # The `for` completed without break — we processed the full batch.
                continue
        # Reached only if we `break`'d out due to CAPTCHA. Loop reopens context.

    stats.total_reviews_extracted += total_reviews
    stats.total_reviews_saved += total_reviews
    logger.info(
        "phase_2_complete",
        city=city.name,
        reviews_extracted=total_reviews,
    )

    # Phase 3 — photo download.
    if config.skip_photos:
        logger.info("phase_3_skipped", city=city.name, reason="skip_photos flag")
    else:
        logger.info(
            "phase_3_start", city=city.name, description="Downloading photos"
        )
        backoff.reset()

        # Resume: skip places that already have photos in the DB.
        already_have_photos = (
            set()
            if config.force_rescrape
            else await store.get_place_ids_with_photos()
        )
        photo_pending = [
            r for r in restrooms if r.place_id not in already_have_photos
        ]
        logger.info(
            "phase_3_plan",
            city=city.name,
            total=len(restrooms),
            already_done=len(restrooms) - len(photo_pending),
            to_download=len(photo_pending),
        )

        j = 0
        total_photos = 0
        while j < len(photo_pending):
            batch = photo_pending[j : j + recycle_n]
            async with make_browser_mgr() as context:
                for place in batch:
                    await rate_limiter.acquire()
                    logger.info(
                        "downloading_place_photos",
                        name=place.name,
                        city=city.name,
                        progress=f"{j + 1}/{len(photo_pending)}",
                    )
                    try:
                        photo_filenames = await scrape_photos_for_place(
                            place, context, config, city
                        )
                    except Exception as e:
                        logger.error(
                            "photo_download_error",
                            name=place.name,
                            error=str(e),
                        )
                        photo_filenames = []

                    if photo_filenames:
                        place.downloaded_photos = photo_filenames
                        total_photos += len(photo_filenames)

                    await store.upsert_restroom(place)
                    j += 1
                    await random_delay(config)

        stats.total_photos_downloaded += total_photos
        logger.info(
            "phase_3_complete",
            city=city.name,
            photos_downloaded=total_photos,
        )

    stats.cities_processed.append(city.name)
    logger.info("city_processing_complete", city=city.name)


async def run_pipeline(config: ExtractionConfig) -> ExtractionStats:
    """Run the full extraction pipeline for all configured cities.

    Args:
        config: Extraction configuration with list of cities.

    Returns:
        Aggregated extraction statistics.
    """
    stats = ExtractionStats(
        started_at=datetime.now(),
    )

    store = SQLiteStore(config)
    await store.initialize()
    logger.info(
        "pipeline_started",
        cities=[c.name for c in config.cities],
    )

    # Process each city sequentially
    for city_idx, city in enumerate(config.cities):
        logger.info(
            "city_start",
            city=city.name,
            progress=f"{city_idx + 1}/{len(config.cities)}",
        )
        stats.queries_used.extend(city.search_queries)

        await _process_city(city, config, store, stats)

    # Phase 4 — CSV export (all cities combined).
    logger.info("phase_4_start", description="Exporting to CSV")
    csv_files = await export_to_csv(store, config)
    logger.info("phase_4_complete", exported_files=list(csv_files.keys()))

    stats.finished_at = datetime.now()
    duration = (
        (stats.finished_at - stats.started_at).total_seconds()
        if stats.started_at
        else 0
    )
    logger.info(
        "pipeline_complete",
        cities=stats.cities_processed,
        total_restrooms=stats.total_restrooms_saved,
        total_reviews=stats.total_reviews_saved,
        total_photos=stats.total_photos_downloaded,
        errors=len(stats.errors),
        duration_seconds=round(duration, 1),
    )
    return stats
