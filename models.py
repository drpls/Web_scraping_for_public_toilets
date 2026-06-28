"""Pydantic models for Google Maps restroom data extraction."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class PublicRestroom(BaseModel):
    """A public restroom entity extracted from Google Maps."""

    place_id: str = Field(..., description="Google Maps place ID")
    name: str = Field(..., description="Name of the restroom")
    city: str = Field(..., description="City where the restroom is located")
    address: str | None = Field(None, description="Full address")
    latitude: float = Field(..., description="Latitude coordinate")
    longitude: float = Field(..., description="Longitude coordinate")
    rating: float | None = Field(None, description="Average rating (1-5)")
    user_ratings_total: int | None = Field(None, description="Total number of ratings")
    is_open: bool | None = Field(None, description="Whether currently open")
    opening_hours_text: str | None = Field(None, description="Opening hours as text")
    phone_number: str | None = Field(None, description="Phone number")
    website: str | None = Field(None, description="Website URL")
    google_maps_url: str | None = Field(None, description="Google Maps URL")
    category: str | None = Field(None, description="Google Maps category")
    photo_references: list[str] = Field(
        default_factory=list, description="Photo reference IDs from search results"
    )
    downloaded_photos: list[str] = Field(
        default_factory=list,
        description="Filenames of downloaded photos (relative to the place folder)",
    )
    extracted_at: datetime = Field(
        default_factory=datetime.now, description="Extraction timestamp"
    )
    search_query: str | None = Field(
        None, description="The search query that found this restroom"
    )

    @field_validator("rating")
    @classmethod
    def validate_rating(cls, v: float | None) -> float | None:
        if v is not None and (v < 0 or v > 5):
            raise ValueError(f"Rating must be between 0 and 5, got {v}")
        return v

    @field_validator("user_ratings_total")
    @classmethod
    def validate_user_ratings_total(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError(f"User ratings total must be >= 0, got {v}")
        return v


class Review(BaseModel):
    """A review extracted from a Google Maps place page."""

    review_id: str = Field(
        ..., description="Unique review ID (hash of place_id + author + date)"
    )
    place_id: str = Field(..., description="Google Maps place ID")
    author_name: str = Field(..., description="Name of the review author")
    rating: int = Field(..., description="Star rating (1-5)")
    text: str = Field("", description="Review text content")
    published_at: datetime | None = Field(
        None, description="When the review was published"
    )
    extracted_at: datetime = Field(
        default_factory=datetime.now, description="Extraction timestamp"
    )

    @field_validator("rating")
    @classmethod
    def validate_rating(cls, v: int) -> int:
        if v < 1 or v > 5:
            raise ValueError(f"Rating must be between 1 and 5, got {v}")
        return v


class ExtractionStats(BaseModel):
    """Statistics about an extraction run."""

    total_restrooms_found: int = 0
    total_restrooms_saved: int = 0
    total_reviews_extracted: int = 0
    total_reviews_saved: int = 0
    total_photos_downloaded: int = 0
    cities_processed: list[str] = Field(default_factory=list)
    queries_used: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None