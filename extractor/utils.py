"""Shared helpers used by both scrapers and the pipeline."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlsplit, urlunsplit

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

_REL_RE = re.compile(
    r"(?:a|an|(\d+))\s+(minute|hour|day|week|month|year)s?\s+ago",
    re.IGNORECASE,
)


def parse_relative_date(text: str, now: datetime | None = None) -> datetime | None:
    """Parse 'a week ago', '3 months ago' into an approximate absolute datetime."""
    if not text:
        return None
    m = _REL_RE.search(text.strip())
    if not m:
        return None
    qty = int(m.group(1)) if m.group(1) else 1
    days = _REL_UNITS[m.group(2).lower()] * qty
    return (now or datetime.now()) - timedelta(days=days)


# Maps embeds the place id in the URL as !1s0x<hex>:0x<hex>.
# This survives across runs and is what we want as a stable primary key.
_PLACE_ID_RE = re.compile(r"!1s(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)")


def extract_place_id_from_url(url: str) -> str | None:
    """Pull the canonical 0x...:0x... place id out of a Maps URL, or None."""
    m = _PLACE_ID_RE.search(url)
    return m.group(1).lower() if m else None


def force_english(url: str) -> str:
    """Append/override hl=en on a URL so Maps renders the English UI.

    English UI is required for our selectors (button[aria-label*='Reviews']) to match.
    """
    parts = urlsplit(url)
    query = dict(p.split("=", 1) for p in parts.query.split("&") if "=" in p) if parts.query else {}
    query["hl"] = "en"
    return urlunsplit(parts._replace(query=urlencode(query)))
