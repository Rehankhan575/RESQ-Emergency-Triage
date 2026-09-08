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


async def _nominatim_request(location: str) -> list:
    """Single Nominatim request. Raises httpx exceptions on failure."""
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(
            _NOMINATIM_URL,
            params={"q": location, "format": "json", "limit": 1},
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

    logger.info(f"Geocoding via Nominatim: {location!r}")

    results = None
    last_exc = None

    for attempt in range(2):  # try once, retry once on failure
        try:
            if attempt > 0:
                await asyncio.sleep(1.0)  # brief back-off before retry
                logger.info(f"Nominatim retry attempt {attempt + 1} for {location!r}")
            results = await _nominatim_request(location)
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

    if not results:
        raise HTTPException(status_code=404, detail=f"Location not found: {location!r}")

    result = {"lat": float(results[0]["lat"]), "lon": float(results[0]["lon"])}
    _cache[key] = result
    logger.info(f"Geocoded {location!r} → {result}")
    return result
