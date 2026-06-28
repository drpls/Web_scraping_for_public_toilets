"""Configuration settings for the Google Maps restroom extractor.

Supports multi-city extraction (Venice + Rome) with per-city search queries,
photo download settings, and conservative anti-bot parameters for long runs.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Load secrets/tunables from a local .env file (if present) into the
# environment before any os.environ lookups below.
load_dotenv()


class CityConfig(BaseModel):
    """Configuration for a single target city."""

    name: str = Field(..., description="City name in English (used as key)")
    native_name: str = Field(..., description="City name in local language (used for folders)")
    latitude: float = Field(..., description="City center latitude")
    longitude: float = Field(..., description="City center longitude")
    search_queries: list[str] = Field(
        ..., description="Search queries to find public restrooms in this city"
    )

# ── Candidate-filtering LLM (OpenRouter) ──────────────────────────────
# The Phase 1.5 filter classifies raw search hits as real restrooms vs.
# false positives. It runs on OpenRouter's free tier, so the key MUST be
# supplied via the environment — never hardcode it here.
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
# NVIDIA Nemotron 3 Super (free) — MoE 120B/12B-active on OpenRouter's free tier.
# Excellent for structured-output classification tasks. The free roster is
# volatile; override via env with any other ':free' model if this one stops
# being free.
OPENROUTER_MODEL = os.environ.get(
    "OPENROUTER_MODEL", "nvidia/nemotron-3-super-120b-a12b:free"
)
# Free-tier hard limit is 20 requests/minute; stay safely under it.
OPENROUTER_RPM = float(os.environ.get("OPENROUTER_RPM", "18"))

# Pre-defined city configurations
VENICE = CityConfig(
    name="Venice",
    native_name="Venezia",
    latitude=45.4408,
    longitude=12.3155,
    search_queries=[
        # Italian terms (primary for Venice)
        "bagni pubblici",
        "bagno pubblico",
        "bagni comunali",
        "toilette pubbliche",
        "servizi igienici pubblici",
        "wc pubblico",
        # English terms (tourist-oriented results)
        "public restroom",
        "public toilet",
        "public bathroom",
        # Combined with city name for broader results
        "bagno pubblico Venezia",
        "public toilet Venice",
        "bagni pubblici Venezia",
    ],
)

ROME = CityConfig(
    name="Rome",
    native_name="Roma",
    latitude=41.9028,
    longitude=12.4964,
    search_queries=[
        # Italian terms
        "bagni pubblici",
        "bagno pubblico",
        "bagni comunali",
        "toilette pubbliche",
        "servizi igienici pubblici",
        "wc pubblico",
        # English terms
        "public restroom",
        "public toilet",
        "public bathroom",
        # Combined with city name
        "bagno pubblico Roma",
        "public toilet Rome",
        "bagni pubblici Roma",
    ],
)

ALL_CITIES: list[CityConfig] = [VENICE, ROME]


class ExtractionConfig(BaseModel):
    """Configuration for the extraction pipeline."""

    # Target cities
    cities: list[CityConfig] = Field(
        default_factory=lambda: list(ALL_CITIES),
        description="List of cities to process",
    )

    # Scraping parameters
    # Safety cap to avoid pathological loops; the natural exit is
    # `no_new_review_scrolls_max` consecutive scrolls without new content.
    max_reviews_per_place: int = Field(
        default=5000, description="Hard cap on reviews per place"
    )
    no_new_review_scrolls_max: int = Field(
        default=6,
        description="Stop scrolling after this many empty scrolls (end-of-list signal)",
    )
    scroll_delay_ms: int = Field(
        default=3500, description="Delay between scroll actions (ms)"
    )
    location_delay_s: float = Field(
        default=20.0, description="Delay between search queries (seconds)"
    )
    action_delay_min_s: float = Field(
        default=3.0, description="Min delay between actions (seconds)"
    )
    action_delay_max_s: float = Field(
        default=8.0, description="Max delay between actions (seconds)"
    )
    review_sort: str = Field(
        default="newest",
        description="Review sort order: newest, relevant, highest, lowest",
    )

    # Photo download settings
    max_photos_per_place: int = Field(
        default=0,
        description="Max photos to download per place (0 = unlimited)",
    )
    photo_download_delay_s: float = Field(
        default=1.5, description="Delay between individual photo downloads (seconds)"
    )
    skip_photos: bool = Field(
        default=False, description="If True, skip photo download entirely"
    )
    max_photo_bytes: int = Field(
        default=500_000,
        description=(
            "Max file size per photo in bytes. Photos exceeding this are "
            "re-compressed as JPEG at progressively lower quality until they fit."
        ),
    )

    # Browser settings
    headless: bool = Field(default=False, description="Run browser in headless mode")
    profile_dir: Path = Field(
        default=Path.home() / ".aleessiaaaa" / "chrome-profile",
        description="Persistent Chrome profile directory",
    )
    viewport_width: int = Field(default=1280, description="Browser viewport width")
    viewport_height: int = Field(default=900, description="Browser viewport height")

    # Storage
    db_path: Path = Field(
        default=Path("data/restrooms.db"), description="SQLite database path"
    )
    output_dir: Path = Field(
        default=Path("data"), description="Root output directory for CSV and photos"
    )
    photos_dir: Path = Field(
        default=Path("data/photos"), description="Root directory for downloaded photos"
    )

    # Rate limiting + long-run hygiene
    max_places_per_minute: float = Field(
        default=2.0, description="Token-bucket cap across all places"
    )
    captcha_backoff_base_s: float = Field(
        default=900.0, description="Base sleep on CAPTCHA detection (seconds)"
    )
    captcha_max_attempts: int = Field(
        default=4,
        description="Give up after this many CAPTCHA backoffs",
    )
    recycle_browser_every_n_places: int = Field(
        default=15,
        description="Close & relaunch browser every N places to drop fingerprint state",
    )
    force_rescrape: bool = Field(
        default=False,
        description="If True, re-scrape places that already have reviews in the DB",
    )
    max_retries: int = Field(default=3, description="Max retries per failed action")


# Default config instance
DEFAULT_CONFIG = ExtractionConfig()