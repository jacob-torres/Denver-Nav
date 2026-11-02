"""Address-only description: streets and businesses around a single address.

Given an address, geocode it, then report
  * the named streets passing within STREET_RADIUS_M (with orientation), and
  * the businesses / landmarks within PLACE_RADIUS_M (with their street
    addresses and compass direction from the address),
as a list of short narrative lines suitable for reading aloud.

Places are fetched with the same Places API v1 Nearby Search and type filters
used for routes, so results are consistent between the two modes.
"""

import asyncio
import math
import re
from concurrent.futures import ThreadPoolExecutor

import requests

from app.address_rules import (
    address_geographic_side,
    block_position_text,
    parse_address_number,
    street_axis,
)
from app.config import GOOGLE_API_KEY
from app.geocoding import _geocode_sync
from app.intersection import (
    _SKIP_HIGHWAY_RE,
    _base_street_name,
    _bearing,
    _bearing_to_compass,
    _closest_point_on_way,
    _dist_m,
    _normalize_way_name,
    _street_orientation,
    OVERPASS_HEADERS,
    OVERPASS_URL,
)
from app.places import (
    FIELD_MASK,
    PLACES_V1_URL,
    _is_wanted,
    _spatial_dedupe,
    is_underground_only,
    short_address,
)

# ── Tuning constants ─────────────────────────────────────────────────────────
STREET_RADIUS_M = 100.0   # named streets considered "around" the address
PLACE_RADIUS_M  = 120.0   # businesses considered "around" the address
SEARCH_RADIUS_M = 90.0    # radius of each Nearby Search call
SEARCH_OFFSET_M = 80.0    # offset of the four outer search centres
MAX_PLACES      = 40      # cap on places reported

_COMPASS_WORDS = {
    "N": "north", "NE": "northeast", "E": "east", "SE": "southeast",
    "S": "south", "SW": "southwest", "W": "west", "NW": "northwest",
}

# "1630 Chestnut Pl" -> number, street name
_ADDR_PARTS_RE = re.compile(r"^(\d+)\s+(.+)$")


# ── Streets ───────────────────────────────────────────────────────────────────

_OVERPASS_MIRRORS = [OVERPASS_URL, "https://overpass.kumi.systems/api/interpreter"]


def _query_ways(lat: float, lng: float) -> list[dict] | None:
    """Named highway ways around the point, or None if every server failed.

    Distinguishes "no streets here" (empty list) from "Overpass is down"
    (None); the public servers are often at capacity.
    """
    q = f"""
[out:json][timeout:15];
way(around:{int(STREET_RADIUS_M)},{lat},{lng})["highway"]["name"];
out body geom;
"""
    for url in _OVERPASS_MIRRORS:
        try:
            r = requests.post(url, data={"data": q},
                              headers=OVERPASS_HEADERS, timeout=20)
            r.raise_for_status()
            return r.json().get("elements", [])
        except Exception as e:
            print(f"[WARNING] OSM way query (describe, {url}): {type(e).__name__}: {e}")
    return None


def _nearby_streets(lat: float, lng: float) -> list[dict] | None:
    """Distinct named streets within STREET_RADIUS_M, nearest first.

    Returns None when the street data could not be fetched.
    """
    ways = _query_ways(lat, lng)
    if ways is None:
        return None
    best: dict[str, dict] = {}
    for w in ways:
        tags = w.get("tags", {})
        if _SKIP_HIGHWAY_RE.match(tags.get("highway", "")):
            continue
        raw = tags.get("name", "").strip()
        geom = w.get("geometry") or []
        if not raw or len(geom) < 2:
            continue
        name = _normalize_way_name(raw)
        dist, plat, plng, seg_b = _closest_point_on_way(lat, lng, geom)
        if dist > STREET_RADIUS_M:
            continue
        key = _base_street_name(name).lower()
        if key in best and best[key]["dist_m"] <= dist:
            continue
        best[key] = {
            "name": name,
            "orientation": _street_orientation(seg_b),
            "bearing": round(seg_b, 1),
            "dist_m": round(dist),
            "direction": (
                _COMPASS_WORDS[_bearing_to_compass(_bearing(lat, lng, plat, plng))]
                if dist >= 5 else "here"
            ),
        }
    return sorted(best.values(), key=lambda s: s["dist_m"])


# ── Places ────────────────────────────────────────────────────────────────────

def _offset(lat: float, lng: float, north_m: float, east_m: float) -> tuple[float, float]:
    return (
        lat + north_m / 111_000,
        lng + east_m / (111_000 * math.cos(math.radians(lat))),
    )


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
        print(f"[WARNING] Places API (describe): {type(e).__name__}: {e}")
        return []


def _nearby_places(lat: float, lng: float) -> list[dict]:
    """Businesses within PLACE_RADIUS_M, nearest first.

    One search can return at most 20 results, so a dense block would be cut
    off.  Five overlapping searches (centre + N/E/S/W) are merged instead.
    """
    centres = [(lat, lng)] + [
        _offset(lat, lng, n * SEARCH_OFFSET_M, e * SEARCH_OFFSET_M)
        for n, e in ((1, 0), (0, 1), (-1, 0), (0, -1))
    ]
    with ThreadPoolExecutor(max_workers=len(centres)) as pool:
        batches = list(pool.map(lambda c: _nearby_search(*c), centres))

    seen: set[str] = set()
    results: list[dict] = []
    for place in (p for batch in batches for p in batch):
        pid = place.get("id", "")
        if not pid or pid in seen:
            continue
        seen.add(pid)

        if place.get("businessStatus") == "CLOSED_PERMANENTLY":
            continue
        loc = place.get("location") or {}
        plat, plng = loc.get("latitude"), loc.get("longitude")
        if plat is None or plng is None:
            continue
        dist = _dist_m(lat, lng, plat, plng)
        if dist > PLACE_RADIUS_M:
            continue

        types = set(place.get("types", []))
        name = (place.get("displayName") or {}).get("text", "").strip()
        if not name or not _is_wanted(types):
            continue

        entry = {
            "name": name,
            "lat": plat,
            "lng": plng,
            "types": sorted(types),
            "address": short_address(place.get("formattedAddress", "")),
            "dist_m": round(dist),
            "direction": (
                _COMPASS_WORDS[_bearing_to_compass(_bearing(lat, lng, plat, plng))]
                if dist >= 5 else "here"
            ),
            # _spatial_dedupe sorts on this key; distance serves here.
            "dist_along_route": dist,
        }
        if is_underground_only(entry):
            continue
        results.append(entry)

    results = _spatial_dedupe(results)
    return results[:MAX_PLACES]


# ── Narrative ─────────────────────────────────────────────────────────────────

# "Ste 125", "Suite B", "#120", "Unit 4" and anything after
_UNIT_RE = re.compile(r"\s*(?:#|\b(?:ste|suite|unit|apt|fl|floor|room|rm)\b\.?).*$", re.IGNORECASE)


def _street_of(address: str) -> str:
    """Street name of an address, without number or suite."""
    m = _ADDR_PARTS_RE.match(address.strip())
    return _UNIT_RE.sub("", m.group(2) if m else address.strip())


def _address_line(address: str, streets: list[dict]) -> str | None:
    """Which side of its street, and how far into the block, the address is."""
    number = parse_address_number(address)
    if number is None or not streets or not _ADDR_PARTS_RE.match(address.strip()):
        return None
    street_name = _base_street_name(_street_of(address)).lower()
    for s in streets:
        if _base_street_name(s["name"]).lower() == street_name:
            axis = street_axis(s["bearing"])
            side = address_geographic_side(number, axis)
            return (
                f"{number} is on the {_COMPASS_WORDS[side]} side of "
                f"{_street_of(address)}, {block_position_text(number)}."
            )
    return None


def _place_text(p: dict) -> str:
    where = "right at the address" if p["direction"] == "here" else (
        f"{p['dist_m']} meters {p['direction']}"
    )
    addr = f", {p['address']}" if p["address"] else ""
    return f"{p['name']}{addr}, {where}"


def _build_narrative(streets: list[dict] | None, places: list[dict]) -> list[str]:
    lines: list[str] = []

    if streets is None:
        lines.append("Street layout data is temporarily unavailable, so "
                     "nearby streets are not listed. Try again in a minute.")
    elif streets:
        lines.append(
            "Streets nearby: " + "; ".join(
                f"{s['name']}, which {s['orientation']}"
                + ("" if s["direction"] == "here" else
                   f", {s['dist_m']} meters {s['direction']}")
                for s in streets
            ) + "."
        )
    else:
        lines.append("No named streets were found within "
                     f"{int(STREET_RADIUS_M)} meters.")

    if not places:
        lines.append(f"No businesses or landmarks were found within "
                     f"{int(PLACE_RADIUS_M)} meters.")
        return lines

    # Group by street, ordering streets by their nearest place and each
    # street's businesses by house number so they read in walking order.
    groups: dict[str, list[dict]] = {}
    for p in places:
        groups.setdefault(_street_of(p["address"]) or "Other", []).append(p)

    def house_no(p: dict) -> int:
        return parse_address_number(p["address"]) or 0

    for street, group in sorted(
        groups.items(), key=lambda kv: min(p["dist_m"] for p in kv[1])
    ):
        group.sort(key=lambda p: (house_no(p), p["dist_m"]))
        lines.append(
            f"On {street}: " + "; ".join(_place_text(p) for p in group) + "."
        )
    return lines


# ── Entry point ───────────────────────────────────────────────────────────────

def _describe_sync(address: str) -> dict:
    geo = _geocode_sync(address)
    lat, lng = geo["lat"], geo["lng"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_streets = pool.submit(_nearby_streets, lat, lng)
        f_places = pool.submit(_nearby_places, lat, lng)
        streets, places = f_streets.result(), f_places.result()

    short = short_address(geo["formatted_address"])
    narrative = _build_narrative(streets, places)
    side_line = _address_line(short, streets)
    if side_line:
        narrative.insert(0, side_line)

    return {
        "address": geo["formatted_address"],
        "lat": lat,
        "lng": lng,
        "streets": streets or [],
        "streets_available": streets is not None,
        "places": [
            {k: p[k] for k in ("name", "address", "dist_m", "direction", "lat", "lng", "types")}
            for p in places
        ],
        "narrative": narrative,
    }


async def describe_address(address: str) -> dict:
    return await asyncio.to_thread(_describe_sync, address)
