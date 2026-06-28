"""OpenRouter-based filter to classify Google Maps results as actual restrooms.

After Phase 1 discovers candidates, many are false positives (parks, parking
garages, train stations, etc.). This module sends the candidate list to a free
model on OpenRouter (DeepSeek V3 by default) and gets back a structured JSON
classification.

The free tier is capped at 20 requests/minute, so calls are paced by a simple
RPM limiter and 429 responses are retried with exponential backoff. On any
unrecoverable error the affected batch is kept wholesale (better to keep a false
positive than to drop a real restroom).
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import structlog
from pydantic import BaseModel, Field, ValidationError

from config import OPENROUTER_API_KEY, OPENROUTER_MODEL, OPENROUTER_RPM
from models import PublicRestroom

logger = structlog.get_logger(__name__)

_API_URL = "https://openrouter.ai/api/v1/chat/completions"

# Gemini-style token limits don't apply here, but keeping batches bounded keeps
# each response small enough that free models reliably return complete JSON.
_BATCH_SIZE = 40

# How long to wait for one classification call.
_REQUEST_TIMEOUT_S = 120.0

# ── Pydantic schema for parsing the model's JSON output ───────────────


class PlaceClassification(BaseModel):
    """Classification result for a single candidate place."""

    place_id: str = Field(..., description="The place_id from the input list")
    name: str = Field("", description="The name from the input list")
    is_restroom: bool = Field(
        ...,
        description="True if this place is a public restroom/toilet/WC.",
    )
    reason: str = Field("", description="Brief justification")


class FilterResult(BaseModel):
    """Structured output from the classification call."""

    classifications: list[PlaceClassification] = Field(default_factory=list)


# ── Prompt ────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a classification assistant. You receive a list of Google Maps places
(name, address, category) that were returned by searching for public restrooms
in an Italian city. Classify each place as either an actual public
restroom/toilet/WC/bathroom facility (is_restroom=true) or NOT a restroom
(is_restroom=false).

Rules:
- A place IS a restroom if its primary function is providing toilet/bathroom
  facilities to the public. This includes: "Bagni pubblici", "WC", "Public
  restroom", "Public toilet", "Toilette", "Bagno pubblico", "Gabinetto",
  "City toilet", "Paid toilet", etc.
- A place is NOT a restroom if it is a park, garden, parking garage, train
  station, ferry terminal, church, restaurant, hotel, people mover, waste
  collection point, beach cabins, or any other venue that merely happens to
  have a restroom inside. The key distinction: is the place's PRIMARY purpose
  to be a restroom?
- When in doubt, lean towards is_restroom=true — it is better to include a
  borderline case than to miss a real restroom.
- You MUST classify every single place in the input list. Do not skip any.

Respond with ONLY a JSON object of this exact shape, no markdown, no prose:
{"classifications": [{"place_id": "...", "name": "...", "is_restroom": true,
"reason": "..."}]}
"""


class _RpmLimiter:
    """Spaces out requests to stay under a requests-per-minute ceiling."""

    def __init__(self, rpm: float) -> None:
        self._min_interval = 60.0 / rpm if rpm > 0 else 0.0
        self._last_call = 0.0

    async def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        wait = self._min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call = time.monotonic()


def _format_candidates(candidates: list[PublicRestroom]) -> str:
    """Format candidate places into a numbered list for the prompt."""
    lines: list[str] = []
    for i, c in enumerate(candidates, 1):
        parts = [f"{i}. place_id={c.place_id}", f'name="{c.name}"']
        if c.address:
            parts.append(f'address="{c.address}"')
        if c.category:
            parts.append(f'category="{c.category}"')
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def _parse_content(content: str) -> FilterResult:
    """Parse the model's text response into a FilterResult.

    Free models sometimes wrap JSON in markdown fences or add stray prose, so
    we strip fences and fall back to extracting the outermost JSON object.
    """
    text = content.strip()
    if text.startswith("```"):
        # Drop the opening fence (``` or ```json) and the closing fence.
        text = text.split("\n", 1)[-1] if "\n" in text else text
        if text.endswith("```"):
            text = text[: -3]
        text = text.strip()

    try:
        return FilterResult.model_validate_json(text)
    except ValidationError:
        pass

    # Last resort: grab from the first '{' to the last '}'.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return FilterResult.model_validate(json.loads(text[start : end + 1]))
    raise ValueError("Could not parse classification JSON from model response")


async def _classify_batch(
    client: httpx.AsyncClient,
    batch: list[PublicRestroom],
) -> FilterResult:
    """Classify one batch, retrying on 429 rate-limit responses."""
    user_prompt = (
        f"Classify each of these {len(batch)} Google Maps places. "
        f"For each one, determine if it is an actual public restroom.\n\n"
        f"{_format_candidates(batch)}"
    )
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }

    max_retries = 3
    for attempt in range(max_retries + 1):
        resp = await client.post(
            _API_URL, json=payload, headers=headers, timeout=_REQUEST_TIMEOUT_S
        )
        if resp.status_code == 429 and attempt < max_retries:
            wait = 60 * (2 ** attempt)  # 60s, 120s, 240s
            logger.warning(
                "openrouter_rate_limited", attempt=attempt + 1, wait_seconds=wait
            )
            await asyncio.sleep(wait)
            continue
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return _parse_content(content)

    raise RuntimeError("OpenRouter returned 429 after all retries")


async def filter_restrooms_with_openrouter(
    candidates: list[PublicRestroom],
) -> list[PublicRestroom]:
    """Filter a list of candidate places, keeping only actual restrooms.

    Calls a free OpenRouter model to classify each candidate. Falls back to
    keeping all candidates in a batch if its API call fails.

    Args:
        candidates: List of PublicRestroom candidates from discovery.

    Returns:
        Filtered list containing only places classified as restrooms.
    """
    if not candidates:
        return []

    if not OPENROUTER_API_KEY:
        logger.warning(
            "openrouter_key_not_set",
            msg="Set OPENROUTER_API_KEY to enable filtering; keeping all candidates",
        )
        return candidates

    limiter = _RpmLimiter(OPENROUTER_RPM)
    approved_ids: set[str] = set()

    async with httpx.AsyncClient() as client:
        for batch_start in range(0, len(candidates), _BATCH_SIZE):
            batch = candidates[batch_start : batch_start + _BATCH_SIZE]
            logger.info(
                "openrouter_filter_batch",
                batch_start=batch_start,
                batch_size=len(batch),
                total=len(candidates),
                model=OPENROUTER_MODEL,
            )

            await limiter.acquire()
            try:
                result = await _classify_batch(client, batch)

                batch_approved = 0
                batch_rejected = 0
                classified_ids = set()
                for c in result.classifications:
                    classified_ids.add(c.place_id)
                    if c.is_restroom:
                        approved_ids.add(c.place_id)
                        batch_approved += 1
                    else:
                        batch_rejected += 1
                        logger.debug(
                            "openrouter_rejected", name=c.name, reason=c.reason
                        )

                # Safety net: if the model silently dropped any place, keep it
                # rather than losing a potential restroom.
                for c in batch:
                    if c.place_id not in classified_ids:
                        approved_ids.add(c.place_id)
                        logger.debug("openrouter_unclassified_kept", name=c.name)

                logger.info(
                    "openrouter_filter_batch_complete",
                    approved=batch_approved,
                    rejected=batch_rejected,
                )
            except Exception as e:
                logger.error(
                    "openrouter_filter_error",
                    error=str(e),
                    msg="Keeping entire batch as fallback",
                )
                for c in batch:
                    approved_ids.add(c.place_id)

    filtered = [c for c in candidates if c.place_id in approved_ids]
    logger.info(
        "openrouter_filter_complete",
        input_count=len(candidates),
        output_count=len(filtered),
        rejected=len(candidates) - len(filtered),
    )
    return filtered
