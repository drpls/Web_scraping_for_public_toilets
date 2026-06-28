"""CSV exporter: exports SQLite data to CSV files.

Produces two CSV files:
  - restrooms.csv: all restrooms with an `images` column listing downloaded photo
    filenames separated by '|'.
  - reviews.csv: all reviews across all cities.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import structlog

from config import ExtractionConfig
from storage.sqlite_store import SQLiteStore

logger = structlog.get_logger(__name__)


async def export_to_csv(
    store: SQLiteStore,
    config: ExtractionConfig,
) -> dict[str, Path]:
    """Export restrooms and reviews tables to CSV files.

    The restrooms CSV includes an `images` column with pipe-separated filenames
    of downloaded photos for each restroom.

    Args:
        store: SQLite store instance.
        config: Extraction configuration.

    Returns:
        Dict mapping table name to CSV file path.
    """
    config.output_dir.mkdir(parents=True, exist_ok=True)
    exported: dict[str, Path] = {}

    # Export restrooms
    restrooms = await store.get_restrooms()
    if restrooms:
        df_restrooms = pd.DataFrame(restrooms)

        # Convert downloaded_photos JSON list to pipe-separated filenames column
        def _photos_to_pipe(val: str | None) -> str:
            if not val:
                return ""
            try:
                photos = json.loads(val)
                if isinstance(photos, list):
                    return "|".join(photos)
            except (json.JSONDecodeError, TypeError):
                pass
            return ""

        if "downloaded_photos" in df_restrooms.columns:
            df_restrooms["images"] = df_restrooms["downloaded_photos"].apply(
                _photos_to_pipe
            )
            df_restrooms = df_restrooms.drop(columns=["downloaded_photos"])
        else:
            df_restrooms["images"] = ""

        # Also convert photo_references JSON to pipe-separated for readability
        if "photo_references" in df_restrooms.columns:
            df_restrooms["photo_references"] = df_restrooms[
                "photo_references"
            ].apply(
                lambda v: "|".join(json.loads(v))
                if v and v != "[]"
                else ""
            )

        path = config.output_dir / "restrooms.csv"
        df_restrooms.to_csv(path, index=False, encoding="utf-8")
        exported["restrooms"] = path
        logger.info("restrooms_exported", path=str(path), count=len(restrooms))
    else:
        logger.warning("no_restrooms_to_export")

    # Export reviews
    reviews = await store.get_reviews()
    if reviews:
        df_reviews = pd.DataFrame(reviews)
        path = config.output_dir / "reviews.csv"
        df_reviews.to_csv(path, index=False, encoding="utf-8")
        exported["reviews"] = path
        logger.info("reviews_exported", path=str(path), count=len(reviews))
    else:
        logger.warning("no_reviews_to_export")

    return exported