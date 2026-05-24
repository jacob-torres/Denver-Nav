"""Find Google Places POIs along a route and determine which side of the
street each one is on, using the Places API v1 (Nearby Search).

Approach:
1. Sample the route every SAMPLE_INTERVAL_M metres (capped at MAX_SAMPLES).
2. Fire parallel POST requests to places:searchNearby for each sample point.
3. Deduplicate by place id, filter to within SNAP_TOL_M of the route
   centreline, apply type filtering, and compute left/right side via the
   same cross-product geometry used for OSM crossings.
"""

import asyncio
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from shapely.geometry import Point

from app.config import GOOGLE_API_KEY
from app.crossings import METERS_PER_DEG, build_route_linestring, compute_side

PLACES_V1_URL = "https://places.googleapis.com/v1/places:searchNearby"

FIELD_MASK = (
    "places.id,"
    "places.displayName,"
    "places.formattedAddress,"
    "places.location,"
    "places.types,"
    "places.businessStatus,"
    "places.rating"
)

# ── Tuning constants ─────────────────────────────────────────────────────────
SNAP_TOL_M        = 40.0   # keep places within this many metres of route
SEARCH_RADIUS_M   = 55.0   # Nearby Search radius per sample point
SAMPLE_INTERVAL_M = 100.0  # target spacing between sample points
MAX_SAMPLES       = 30     # cap API calls on very long routes

# ── Type allow-list ──────────────────────────────────────────────────────────
# A place passes only if its types intersect this set.
INCLUDE_TYPES: set[str] = {
    # Food & drink
    "restaurant", "cafe", "bar", "bakery", "food",
    "meal_takeaway", "night_club", "liquor_store",
    # Health & medical
    "pharmacy", "hospital", "doctor", "dentist", "health",
    "drugstore", "medical_lab",
    # Finance
    "bank", "atm", "finance",
    # Retail
    "grocery_store", "grocery_or_supermarket", "supermarket",
    "convenience_store", "gas_station", "department_store",
    "shopping_mall", "clothing_store", "electronics_store",
    "book_store", "furniture_store", "home_goods_store",
    "hardware_store", "pet_store", "jewelry_store", "shoe_store",
    "bicycle_store", "gift_shop", "florist",
    # Personal services
    "laundry", "hair_care", "beauty_salon", "spa", "car_repair",
    "car_wash", "dry_cleaning", "insurance_agency",
    # Civic / government
    "post_office", "library", "fire_station", "police",
    "courthouse", "city_hall", "embassy",
    "government_office", "local_government_office",
    # Education
    "school", "university", "primary_school", "secondary_school",
    # Entertainment / recreation
    "gym", "stadium", "sports_complex", "sports_club",
    "museum", "art_gallery", "movie_theater",
    "tourist_attraction", "amusement_park", "aquarium",
    "bowling_alley",
    # Religion
    "place_of_worship", "church", "mosque", "synagogue",
    # Transport
    "transit_station", "bus_station", "train_station",
    "subway_station", "light_rail_station",
    # Real hotels (not short-term rentals)
    "hotel", "motel",
    # Parks & outdoors
    "park", "natural_feature", "campground",
    # Other establishments
    "funeral_home", "veterinary_care", "storage",
}

# ── Type deny-patterns ───────────────────────────────────────────────────────
# If ALL non-generic types match one of these, the place is skipped.
# Handles Airbnbs / apartment listings that leak through as "lodging".
_RESIDENTIAL_TYPES: set[str] = {
    "lodging",              # short-term rentals — real hotels also have "hotel"
    "apartment_complex",
    "condominium_complex",
    "real_estate_agency",
}
_HOTEL_TYPES: set[str] = {"hotel", "motel", "resort", "inn", "hostel"}

# Pure administrative / geographic — skip if ALL types are only these.
_ADMIN_TYPES: set[str] = {
    "route", "street_address", "political", "sublocality",
    "sublocality_level_1", "neighborhood", "premise", "postal_code",
    "country", "administrative_area_level_1", "administrative_area_level_2",
    "administrative_area_level_3", "colloquial_area", "locality",
    "plus_code",
}
# Generic labels that appear on everything — don't count as qualifying.
_GENERIC: set[str] = {"point_of_interest", "establishment"}


def _is_wanted(types: set[str]) -> bool:
    """Return True if this place should appear in the route."""
    meaningful = types - _GENERIC - _ADMIN_TYPES

    # Skip if purely administrative.
    if types and types.issubset(_ADMIN_TYPES | _GENERIC):
        return False

    # Skip short-term rentals: have "lodging" but not an actual hotel type.
    if "lodging" in types and not (types & _HOTEL_TYPES):
        return False

    # Skip apartment / condo complexes.
    if meaningful & _RESIDENTIAL_TYPES:
        return False

    # Must match at least one include type.
    if meaningful and not (meaningful & INCLUDE_TYPES):
        return False

    return True


# ── Spatial deduplication ─────────────────────────────────────────────────────

def _dist_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    dlat = (lat2 - lat1) * 111_000
    dlng = (lng2 - lng1) * math.cos(math.radians((lat1 + lat2) / 2)) * 111_000
    return math.hypot(dlat, dlng)


def _spatial_dedupe(places: list[dict]) -> list[dict]:
    """Remove near-duplicate place entries.

    Two passes:
      1. Same name within 60 m → keep only the first (handles multiple bus stop
         nodes for the same intersection reported by different routes/directions).
      2. Any name within 8 m → keep only the shorter/simpler name (handles a
         bank, its ATM, and its mortgage office all geocoded to the same door).
    """
    # Pass 1: same-name dedup within 60 m.
    seen_name: list[dict] = []
    for p in places:
        duplicate = False
        for q in seen_name:
            if q["name"] == p["name"] and _dist_m(p["lat"], p["lng"], q["lat"], q["lng"]) < 60.0:
                duplicate = True
                break
        if not duplicate:
            seen_name.append(p)

    # Pass 2: proximity dedup within 8 m — keep shorter name (more likely the
    # primary establishment rather than a sub-service).  8 m catches an ATM
    # geocoded to the same door as its bank without collapsing distinct businesses
    # in adjacent storefronts (e.g. two barber shops in the same building).
    seen_name.sort(key=lambda p: len(p["name"]))  # shorter names first
    seen_prox: list[dict] = []
    for p in seen_name:
        too_close = any(
            _dist_m(p["lat"], p["lng"], q["lat"], q["lng"]) < 8.0
            for q in seen_prox
        )
        if not too_close:
            seen_prox.append(p)

    seen_prox.sort(key=lambda p: p["dist_along_route"])
    return seen_prox


# ── Sampling ─────────────────────────────────────────────────────────────────

def _sample_route(route_line) -> list[tuple[float, float]]:
    length_m = route_line.length * METERS_PER_DEG
    n = max(2, min(MAX_SAMPLES, math.ceil(length_m / SAMPLE_INTERVAL_M) + 1))
    pts = []
    for i in range(n):
        t = i / (n - 1)
        p = route_line.interpolate(t, normalized=True)
        pts.append((p.y, p.x))   # (lat, lng)
    return pts


# ── Single request ────────────────────────────────────────────────────────────

def _nearby_search(lat: float, lng: float) -> list[dict]:
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_API_KEY,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    body = {
        "locationRestriction": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": SEARCH_RADIUS_M,
            }
        },
        "maxResultCount": 20,
    }
    try:
        r = requests.post(PLACES_V1_URL, json=body, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json().get("places", [])
    except Exception as e:
        print(f"[WARNING] Places API: {type(e).__name__}: {e}")
        return []


# ── Main blocking fetch ───────────────────────────────────────────────────────

def _fetch_places_sync(route_points: list[tuple[float, float]]) -> list[dict]:
    route_line = build_route_linestring(route_points)
    sample_pts = _sample_route(route_line)
    snap_tol_deg = SNAP_TOL_M / METERS_PER_DEG

    # Parallel requests.
    raw: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(len(sample_pts), 10)) as pool:
        futures = [pool.submit(_nearby_search, lat, lng) for lat, lng in sample_pts]
        for f in as_completed(futures):
            raw.extend(f.result())

    # Deduplicate by place id.
    seen: set[str] = set()
    unique: list[dict] = []
    for place in raw:
        pid = place.get("id", "")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        unique.append(place)

    # Filter, compute positions and sides.
    results: list[dict] = []
    for place in unique:
        if place.get("businessStatus") == "CLOSED_PERMANENTLY":
            continue

        loc = place.get("location") or {}
        lat = loc.get("latitude")
        lng = loc.get("longitude")
        if lat is None or lng is None:
            continue

        pt = Point(lng, lat)
        if route_line.distance(pt) > snap_tol_deg:
            continue

        types: set[str] = set(place.get("types", []))
        if not _is_wanted(types):
            continue

        name = (place.get("displayName") or {}).get("text", "").strip()
        if not name:
            continue

        dist_along = route_line.project(pt, normalized=True)
        side = compute_side(route_line, pt)

        results.append({
            "dist_along_route": dist_along,
            "lat": lat,
            "lng": lng,
            "name": name,
            "vicinity": short_address(place.get("formattedAddress", "")),
            "types": sorted(types),
            "side": side,
            "place_id": place.get("id", ""),
            "rating": place.get("rating"),
        })

    results.sort(key=lambda p: p["dist_along_route"])
    results = _spatial_dedupe(results)
    return results


# ── Async entry point ─────────────────────────────────────────────────────────

async def find_places_along_route(route_points: list[tuple[float, float]]) -> list[dict]:
    if len(route_points) < 2:
        return []
    return await asyncio.to_thread(_fetch_places_sync, route_points)


# ── Underground / gate filter (shared with routing and intersection) ───────────

_TRANSIT_STATION_TYPES: set[str] = {
    "transit_station", "bus_station", "train_station",
    "light_rail_station", "subway_station",
}

# If a transit-typed place ALSO has one of these types it has a real
# above-ground establishment co-located (e.g. Whole Foods inside Union Station).
_ABOVE_GROUND_COLOCATED_TYPES: set[str] = {
    "restaurant", "cafe", "bar", "bakery", "food", "meal_takeaway",
    "grocery_store", "grocery_or_supermarket", "supermarket",
    "convenience_store", "pharmacy", "drugstore", "bank", "atm",
    "hotel", "motel", "gym", "museum", "art_gallery", "movie_theater",
    "shopping_mall", "department_store", "clothing_store",
    "electronics_store", "book_store", "hair_care", "beauty_salon",
    "post_office", "library", "night_club", "liquor_store",
}


_STREET_NUM_RE = re.compile(r"^\d+\s+[A-Za-z]")


def short_address(formatted: str) -> str:
    """Return only the street number + name from a full formatted address.

    Scans each comma-separated segment and returns the first one that begins
    with one or more digits followed by a space and a letter — the signature
    of a real street address.  This skips leading place-name segments so that:

      "Union Station, 1701 Wynkoop St, Denver, CO ..."  ->  "1701 Wynkoop St"
      "Denver Dry Goods Co, 1600 California St, ..."    ->  "1600 California St"
      "1801 Wewatta St, Denver, CO ..."                 ->  "1801 Wewatta St"

    Falls back to the first segment when no numeric-street segment is found
    (e.g. a place with no street number on record).
    """
    if not formatted:
        return ""
    parts = [p.strip() for p in formatted.split(",")]
    for part in parts:
        if _STREET_NUM_RE.match(part):
            return part
    return parts[0] if parts else ""


def is_underground_only(place: dict) -> bool:
    """Return True when this place is only meaningful underground.

    Two sub-cases:
    1. Terminal gate entries — name contains 'gate' AND the place is a transit
       node (e.g. 'Gate B4', 'RTD Gate A2').
    2. Pure transit hub with no above-ground co-tenancy — Union Station has
       types transit_station/train_station only, so it is underground-only.
       Whole Foods at the same address has grocery_store, so it passes through.
    """
    name  = place.get("name", "").lower()
    types = set(place.get("types", []))
    if not (types & _TRANSIT_STATION_TYPES):
        return False
    if re.search(r"\bgate\b", name):
        return True
    if not (types & _ABOVE_GROUND_COLOCATED_TYPES):
        return True
    return False
