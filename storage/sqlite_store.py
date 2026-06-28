"""SQLite storage for restrooms and reviews.

Provides async CRUD operations for the restroom and review tables.
Used as an intermediate cache for resumable scraping — final output is CSV.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import aiosqlite
import structlog

from config import ExtractionConfig
from models import PublicRestroom, Review

logger = structlog.get_logger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS restrooms (
    place_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT NOT NULL,
    address TEXT,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    rating REAL,
    user_ratings_total INTEGER,
    is_open INTEGER,
    opening_hours_text TEXT,
    phone_number TEXT,
    website TEXT,
    google_maps_url TEXT,
    category TEXT,
    photo_references TEXT,
    downloaded_photos TEXT,
    extracted_at TEXT NOT NULL,
    search_query TEXT
);

CREATE TABLE IF NOT EXISTS reviews (
    review_id TEXT PRIMARY KEY,
    place_id TEXT NOT NULL REFERENCES restrooms(place_id),
    author_name TEXT NOT NULL,
    rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
    text TEXT NOT NULL,
    published_at TEXT,
    extracted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reviews_place_id ON reviews(place_id);
CREATE INDEX IF NOT EXISTS idx_restrooms_city ON restrooms(city);
"""

# Migration: add downloaded_photos column if missing (for existing databases)
MIGRATION_SQL = """
ALTER TABLE restrooms ADD COLUMN downloaded_photos TEXT;
"""


class SQLiteStore:
    """Async SQLite storage for restroom and review data."""

    def __init__(self, config: ExtractionConfig) -> None:
        self.config = config
        self._db_path = config.db_path

    async def initialize(self) -> None:
        """Create the database and tables if they don't exist."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(str(self._db_path)) as db:
            await db.executescript(SCHEMA_SQL)
            await db.commit()

            # Run migration for existing databases
            try:
                await db.execute(MIGRATION_SQL)
                await db.commit()
            except Exception:
                # Column already exists — ignore
                pass

        logger.info("database_initialized", path=str(self._db_path))

    async def upsert_restroom(self, restroom: PublicRestroom) -> None:
        """Insert or update a restroom record."""
        async with aiosqlite.connect(str(self._db_path)) as db:
            await db.execute(
                """
                INSERT INTO restrooms (
                    place_id, name, city, address, latitude, longitude,
                    rating, user_ratings_total, is_open, opening_hours_text,
                    phone_number, website, google_maps_url, category,
                    photo_references, downloaded_photos, extracted_at, search_query
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(place_id) DO UPDATE SET
                    name=excluded.name,
                    address=excluded.address,
                    latitude=excluded.latitude,
                    longitude=excluded.longitude,
                    rating=excluded.rating,
                    user_ratings_total=excluded.user_ratings_total,
                    is_open=excluded.is_open,
                    opening_hours_text=excluded.opening_hours_text,
                    phone_number=excluded.phone_number,
                    website=excluded.website,
                    google_maps_url=excluded.google_maps_url,
                    category=excluded.category,
                    photo_references=excluded.photo_references,
                    downloaded_photos=excluded.downloaded_photos,
                    extracted_at=excluded.extracted_at,
                    search_query=excluded.search_query
                """,
                (
                    restroom.place_id,
                    restroom.name,
                    restroom.city,
                    restroom.address,
                    restroom.latitude,
                    restroom.longitude,
                    restroom.rating,
                    restroom.user_ratings_total,
                    int(restroom.is_open) if restroom.is_open is not None else None,
                    restroom.opening_hours_text,
                    restroom.phone_number,
                    restroom.website,
                    restroom.google_maps_url,
                    restroom.category,
                    json.dumps(restroom.photo_references),
                    json.dumps(restroom.downloaded_photos),
                    restroom.extracted_at.isoformat(),
                    restroom.search_query,
                ),
            )
            await db.commit()
            logger.debug(
                "restroom_upserted",
                place_id=restroom.place_id,
                name=restroom.name,
            )

    async def upsert_review(self, review: Review) -> None:
        """Insert or update a review record."""
        async with aiosqlite.connect(str(self._db_path)) as db:
            await db.execute(
                """
                INSERT INTO reviews (
                    review_id, place_id, author_name, rating, text,
                    published_at, extracted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(review_id) DO UPDATE SET
                    text=excluded.text,
                    rating=excluded.rating
                """,
                (
                    review.review_id,
                    review.place_id,
                    review.author_name,
                    review.rating,
                    review.text,
                    review.published_at.isoformat() if review.published_at else None,
                    review.extracted_at.isoformat(),
                ),
            )
            await db.commit()

    async def upsert_reviews(self, reviews: list[Review]) -> int:
        """Insert or update multiple reviews in a batch.

        Returns:
            Number of reviews upserted.
        """
        if not reviews:
            return 0

        async with aiosqlite.connect(str(self._db_path)) as db:
            await db.executemany(
                """
                INSERT INTO reviews (
                    review_id, place_id, author_name, rating, text,
                    published_at, extracted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(review_id) DO UPDATE SET
                    text=excluded.text,
                    rating=excluded.rating
                """,
                [
                    (
                        r.review_id,
                        r.place_id,
                        r.author_name,
                        r.rating,
                        r.text,
                        r.published_at.isoformat() if r.published_at else None,
                        r.extracted_at.isoformat(),
                    )
                    for r in reviews
                ],
            )
            await db.commit()

        logger.debug("reviews_upserted", count=len(reviews))
        return len(reviews)

    async def get_restrooms(self, city: str | None = None) -> list[dict]:
        """Retrieve restrooms, optionally filtered by city."""
        async with aiosqlite.connect(str(self._db_path)) as db:
            db.row_factory = aiosqlite.Row
            if city:
                cursor = await db.execute(
                    "SELECT * FROM restrooms WHERE city = ?", (city,)
                )
            else:
                cursor = await db.execute("SELECT * FROM restrooms")
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_reviews(self, place_id: str | None = None) -> list[dict]:
        """Retrieve reviews, optionally filtered by place_id."""
        async with aiosqlite.connect(str(self._db_path)) as db:
            db.row_factory = aiosqlite.Row
            if place_id:
                cursor = await db.execute(
                    "SELECT * FROM reviews WHERE place_id = ?", (place_id,)
                )
            else:
                cursor = await db.execute("SELECT * FROM reviews")
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_place_ids_with_reviews(self) -> set[str]:
        """Place ids that already have at least one review — used to skip on resume."""
        async with aiosqlite.connect(str(self._db_path)) as db:
            cursor = await db.execute(
                "SELECT DISTINCT place_id FROM reviews"
            )
            rows = await cursor.fetchall()
            return {row[0] for row in rows}

    async def get_place_ids_with_photos(self) -> set[str]:
        """Place ids that already have downloaded photos — used to skip on resume."""
        async with aiosqlite.connect(str(self._db_path)) as db:
            cursor = await db.execute(
                "SELECT place_id, downloaded_photos FROM restrooms "
                "WHERE downloaded_photos IS NOT NULL AND downloaded_photos != '[]'"
            )
            rows = await cursor.fetchall()
            return {row[0] for row in rows}

    async def get_stats(self) -> dict:
        """Get summary statistics."""
        async with aiosqlite.connect(str(self._db_path)) as db:
            restroom_count = (
                await (await db.execute("SELECT COUNT(*) FROM restrooms")).fetchone()
            )[0]
            review_count = (
                await (await db.execute("SELECT COUNT(*) FROM reviews")).fetchone()
            )[0]
            # Count restrooms with photos
            photo_count = (
                await (
                    await db.execute(
                        "SELECT COUNT(*) FROM restrooms "
                        "WHERE downloaded_photos IS NOT NULL "
                        "AND downloaded_photos != '[]'"
                    )
                ).fetchone()
            )[0]
            # Count by city
            city_cursor = await db.execute(
                "SELECT city, COUNT(*) FROM restrooms GROUP BY city"
            )
            cities = {row[0]: row[1] for row in await city_cursor.fetchall()}

            return {
                "total_restrooms": restroom_count,
                "total_reviews": review_count,
                "restrooms_with_photos": photo_count,
                "cities": cities,
            }