"""
GET /api/geocode?location=<text>

Proxies Nominatim with a proper User-Agent and caches results in-memory.
Returns {"lat": float, "lon": float} or 404 if not found.
"""
import asyncio
import logging
import httpx
from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api", tags=["geocode"])
logger = logging.getLogger("geocode")

# In-memory cache: location string -> {lat, lon}
_cache: dict[str, dict] = {}

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_HEADERS = {
    # Nominatim requires an identifying User-Agent per usage policy.
    # Must NOT be a generic string — include project name + contact/repo.
    "User-Agent": "SIH-EmergencyTriage/1.0 (github.com/rehan/emergency_triage; hackathon-demo)",
    "Accept-Language": "en,hi",
}

# Low-importance threshold — warn but don't block if Nominatim returns an
# ambiguous low-confidence match.
_IMPORTANCE_WARN_THRESHOLD = 0.2

# Case-insensitive substring replacements for common Indian city spelling
# variants that cause wrong-city matches in Nominatim.
# Keys are matched as whole words inside the location string (after lowercasing).
_PLACE_NAME_NORMALIZATIONS: dict[str, str] = {
    "gandhi nagar": "gandhinagar",
}


def _normalize_location(location: str) -> str:
    """
    Apply case-insensitive whole-phrase substitutions from _PLACE_NAME_NORMALIZATIONS.
    The original string is kept as the cache key; only the Nominatim query is affected.
    """
    normalized = location
    lower = location.lower()
    for variant, canonical in _PLACE_NAME_NORMALIZATIONS.items():
        if variant in lower:
            # Replace case-insensitively, preserving surrounding text
            import re
            normalized = re.sub(re.escape(variant), canonical, normalized, flags=re.IGNORECASE)
            logger.info(f"Normalized location spelling: {location!r} → {normalized!r}")
    return normalized


async def _nominatim_request(location: str) -> list:
    """Single Nominatim request. Raises httpx exceptions on failure."""
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(
            _NOMINATIM_URL,
            params={
                "q": location,
                "format": "json",
                "limit": 1,
                "countrycodes": "in",  # restrict to India
            },
            headers=_HEADERS,
        )
        resp.raise_for_status()
        return resp.json()


@router.get("/geocode")
async def geocode(location: str = Query(..., description="Location string to geocode")):
    key = location.strip().lower()
    if key in _cache:
        logger.info(f"Geocode cache hit: {location!r}")
        return _cache[key]

    # Normalize spelling variants before sending to Nominatim,
    # but keep the original string as the cache key.
    query_location = _normalize_location(location.strip())
    logger.info(f"Geocoding via Nominatim: {location!r} (query: {query_location!r})")

    results = None
    last_exc = None

    for attempt in range(2):  # try once, retry once on failure
        try:
            if attempt > 0:
                await asyncio.sleep(1.0)  # brief back-off before retry
                logger.info(f"Nominatim retry attempt {attempt + 1} for {location!r}")
            results = await _nominatim_request(query_location)
            last_exc = None
            break
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            logger.warning(
                f"Nominatim HTTP {exc.response.status_code} for {location!r} "
                f"(attempt {attempt + 1}): {exc}"
            )
        except Exception as exc:
            last_exc = exc
            logger.warning(
                f"Nominatim request failed for {location!r} (attempt {attempt + 1}): {exc}"
            )

    if last_exc is not None:
        raise HTTPException(status_code=502, detail=f"Geocoding failed: {last_exc}")

    if not results and "," in query_location:
        # Fallback: callers often give specific building/room prefixes like "Building C, Karnavati University, Gandhinagar".
        # If the exact sub-building isn't indexed in OpenStreetMap, progressively strip the leftmost segment
        # to geocode the campus, street, or city.
        parts = [p.strip() for p in query_location.split(",") if p.strip()]
        while not results and len(parts) > 1:
            parts.pop(0)
            fallback_query = ", ".join(parts)
            logger.info(f"Nominatim fallback: retrying with broader location: {fallback_query!r}")
            try:
                results = await _nominatim_request(fallback_query)
            except Exception as e:
                logger.warning(f"Nominatim fallback failed for {fallback_query!r}: {e}")
                break

    if not results:
        raise HTTPException(status_code=404, detail=f"Location not found: {location!r}")

    # Low-confidence guard: warn if importance is below threshold
    importance = float(results[0].get("importance", 1.0))
    if importance < _IMPORTANCE_WARN_THRESHOLD:
        logger.warning(
            f"Low-confidence geocode match for {location!r}: "
            f"importance={importance:.4f}, resolved to {results[0].get('display_name')!r}"
        )

    result = {"lat": float(results[0]["lat"]), "lon": float(results[0]["lon"])}
    _cache[key] = result
    logger.info(f"Geocoded {location!r} → {result} (importance={importance:.4f})")
    return result
