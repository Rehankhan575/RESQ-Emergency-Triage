"""
GET /api/config

Returns public runtime config values that the frontend needs,
read from environment variables. Only non-secret, client-safe
values should be exposed here — never DB passwords, private keys, etc.

Thunderforest API keys are semi-public (embedded in tile URLs anyway,
so visible in browser network tab), but keeping them server-side means
they don't get accidentally committed to source control via the HTML file.
"""
import os
from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["config"])


@router.get("/config")
async def get_config():
    return {
        "thunderforest_api_key": os.getenv("THUNDERFOREST_API_KEY", ""),
    }
