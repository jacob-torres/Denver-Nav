import asyncio
import re
import requests
from app.config import GOOGLE_API_KEY, SEARCH_AREA_NAME, SEARCH_BOUNDS

# ── Intersection normalization ─────────────────────────────────────────────────

_INTER_SEP_RE   = re.compile(r"\s+(?:and|at|@|/)\s+", re.IGNORECASE)
_AMP_SPACING_RE = re.compile(r"\s*&\s*")


def normalize_intersection_query(address: str) -> str:
    """Normalize intersection separators to ' & ' (with spaces) for Google APIs.

    '17th and Wewatta'  -> '17th & Wewatta'   (word separator)
    'Colfax / Broadway' -> 'Colfax & Broadway' (slash separator)
    '17th @ Wewatta'    -> '17th & Wewatta'    (at-sign separator)
    '17th & Wewatta'    -> '17th & Wewatta'    (spacing confirmed)
    '17th&Wewatta'      -> '17th & Wewatta'    (spacing added)
    '17th &Wewatta'     -> '17th & Wewatta'    (spacing normalised)
    """
    if "&" in address:
        # Normalise spacing around an existing '&' so Google parses it correctly.
        return _AMP_SPACING_RE.sub(" & ", address)
    return _INTER_SEP_RE.sub(" & ", address)


def _add_area_context(address: str) -> str:
    """Append the search area name to bare landmark queries.

    Checks whether the address already mentions the city (before the comma in
    SEARCH_AREA_NAME) or the state abbreviation (after the comma).  If neither
    is present we append ', Denver, CO' (or whatever area is configured) so
    that 'Union Station' becomes 'Union Station, Denver, CO'.
    """
    parts = [p.strip() for p in SEARCH_AREA_NAME.split(",")]
    city = parts[0].lower() if parts else ""
    state = parts[1].lower() if len(parts) > 1 else ""

    addr_lower = address.lower()
    if city and city in addr_lower:
        return address
    if state and f" {state}" in addr_lower:
        return address
    return f"{address}, {SEARCH_AREA_NAME}"


def _geocode_sync(address: str) -> dict:
    """Synchronous geocode call — run in a thread."""
    if not GOOGLE_API_KEY:
        raise RuntimeError("GOOGLE_API_KEY not set in .env")

    located = _add_area_context(address)
    params = {
        "address": located,
        "key": GOOGLE_API_KEY,
        "bounds": SEARCH_BOUNDS,
    }

    r = requests.get(
        "https://maps.googleapis.com/maps/api/geocode/json",
        params=params,
        timeout=15,
    )
    data = r.json()

    if data["status"] != "OK" or not data.get("results"):
        raise ValueError(
            f"Could not find '{address}' in {SEARCH_AREA_NAME}: {data['status']}"
        )

    result = data["results"][0]
    return {
        "formatted_address": result["formatted_address"],
        "lat": result["geometry"]["location"]["lat"],
        "lng": result["geometry"]["location"]["lng"],
    }


async def geocode_address(address: str) -> dict:
    return await asyncio.to_thread(_geocode_sync, address)
