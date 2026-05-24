"""Intersection orientation lookup for the Denver accessible navigation app.

Given an intersection query ("17th Ave & Wewatta St"), produces:
  - Geocoded centre coordinates + formatted address
  - Street names with local compass orientation (runs N-S, E-W, or diagonally)
  - Signal type (traffic signals / stop sign / marked crossing / uncontrolled)
  - Accessible crossing features (push button, audible signal, tactile paving)
  - Top POIs at each of the four corner quadrants, labelled using the actual
    street bearings so that diagonal-grid intersections get N/E/S/W labels
    rather than always NE/SE/SW/NW.
"""

import asyncio
import math
import re
import requests

from app.config import GOOGLE_API_KEY, SEARCH_BOUNDS
from app.geocoding import _add_area_context, normalize_intersection_query
from app.places import _is_wanted, is_underground_only, short_address, PLACES_V1_URL, FIELD_MASK

GEOCODING_URL = "https://maps.googleapis.com/maps/api/geocode/json"
OVERPASS_URL  = "https://overpass-api.de/api/interpreter"
OVERPASS_HEADERS = {
    "User-Agent": "DenverAccessibleNav/1.0 (accessibility/O&M project)",
    "Accept": "application/json, text/plain, */*",
}

_INTERSECTION_SEARCH_RADIUS_M = 80.0   # wider than the route-sampling radius

# ── Geometry helpers ──────────────────────────────────────────────────────────

def _bearing(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Compass bearing from point 1 to point 2, degrees clockwise from north."""
    d_lng = math.radians(lng2 - lng1)
    lat1r = math.radians(lat1)
    lat2r = math.radians(lat2)
    x = math.sin(d_lng) * math.cos(lat2r)
    y = (math.cos(lat1r) * math.sin(lat2r)
         - math.sin(lat1r) * math.cos(lat2r) * math.cos(d_lng))
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _dist_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    dlat = (lat2 - lat1) * 111_000
    dlng = (lng2 - lng1) * math.cos(math.radians((lat1 + lat2) / 2)) * 111_000
    return math.hypot(dlat, dlng)


def _bearing_to_compass(b: float) -> str:
    """Convert a directed bearing (0-360) to the nearest 8-point compass label."""
    labels = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return labels[int((b % 360 + 22.5) / 45) % 8]


def _street_orientation(undirected_bearing: float) -> str:
    """Human-readable orientation from an undirected bearing (0-180)."""
    b = undirected_bearing % 180
    if b <= 22.5 or b >= 157.5:
        return "runs north-south"
    elif 67.5 <= b <= 112.5:
        return "runs east-west"
    elif 22.5 < b < 67.5:
        return "runs northeast-southwest"
    else:
        return "runs northwest-southeast"


# ── Street-bearing-aware corner classification ────────────────────────────────

def _make_sorted_rays(streets: list[dict]) -> list[float]:
    """Convert two undirected street bearings to four sorted directed rays (0-360).

    Returns an empty list when fewer than two streets are available (falls back
    to simple NE/SE/SW/NW labelling in _classify_corners).
    """
    if len(streets) < 2:
        return []
    t1 = streets[0]["bearing"] % 180   # undirected 0-180
    t2 = streets[1]["bearing"] % 180
    rays = sorted([t1, (t1 + 180) % 360, t2, (t2 + 180) % 360])
    return rays


def _classify_corners(
    raw_places: list[dict],
    center_lat: float,
    center_lng: float,
    sorted_rays: list[float],
) -> dict[str, list[dict]]:
    """Assign each place to one of the four corner quadrants.

    When sorted_rays is available (two streets detected), the quadrant
    boundaries are the actual street directions, so an intersection of streets
    running NE-SW and NW-SE correctly labels its corners N / E / S / W rather
    than NE / SE / SW / NW.

    Falls back to fixed 0/90/180/270 quadrant cuts when street data is absent.
    """
    if sorted_rays:
        # Compute the four corner labels from the arc bisectors.
        n = len(sorted_rays)   # always 4
        corner_labels: list[str] = []
        for i in range(n):
            r_start = sorted_rays[i]
            r_end   = sorted_rays[(i + 1) % n]
            if r_end <= r_start:
                r_end += 360          # wraparound arc
            bisector = (r_start + r_end) / 2
            corner_labels.append(_bearing_to_compass(bisector % 360))

        corners: dict[str, list[dict]] = {lbl: [] for lbl in corner_labels}

        for p in raw_places:
            b     = _bearing(center_lat, center_lng, p["lat"], p["lng"]) % 360
            # Find the arc: sorted_rays[i] <= b < sorted_rays[i+1]
            arc   = n - 1           # default to the wraparound arc
            for i in range(n - 1):
                if sorted_rays[i] <= b < sorted_rays[i + 1]:
                    arc = i
                    break
            # Recompute dist_m from the refined center so ranking is consistent
            # with the bearing used for corner assignment.
            corners[corner_labels[arc]].append({
                "name":    p["name"],
                "dist_m":  round(_dist_m(center_lat, center_lng, p["lat"], p["lng"])),
                "address": p.get("address") or None,
            })
    else:
        # Fallback: simple NE/SE/SW/NW based on 0/90/180/270 boundaries.
        corners = {"NE": [], "SE": [], "SW": [], "NW": []}
        for p in raw_places:
            b = _bearing(center_lat, center_lng, p["lat"], p["lng"])
            if b < 90:
                lbl = "NE"
            elif b < 180:
                lbl = "SE"
            elif b < 270:
                lbl = "SW"
            else:
                lbl = "NW"
            corners[lbl].append({
                "name":    p["name"],
                "dist_m":  round(_dist_m(center_lat, center_lng, p["lat"], p["lng"])),
                "address": p.get("address") or None,
            })

    for key in corners:
        corners[key].sort(key=lambda p: p["dist_m"])
        corners[key] = corners[key][:5]
    return corners


# ── Closest-point-on-segment geometry ────────────────────────────────────────

def _closest_point_on_segment(
    plat: float, plng: float,
    alat: float, alng: float,
    blat: float, blng: float,
) -> tuple[float, float]:
    """Return the closest point on line segment A→B to point P.

    Uses a flat-earth approximation scaled by cos(latitude) for the longitude
    axis.  Accurate to sub-metre for distances up to a few hundred metres.
    """
    cos_lat = math.cos(math.radians((plat + alat) / 2))
    # Offsets from A in approximate metres (east, north)
    px = (plng - alng) * cos_lat * 111_000
    py = (plat - alat) * 111_000
    bx = (blng - alng) * cos_lat * 111_000
    by = (blat - alat) * 111_000

    len_sq = bx * bx + by * by
    if len_sq < 1e-9:
        return alat, alng

    t = max(0.0, min(1.0, (px * bx + py * by) / len_sq))
    return alat + t * (blat - alat), alng + t * (blng - alng)


def _closest_point_on_way(
    clat: float, clng: float, geom: list[dict]
) -> tuple[float, float, float, float]:
    """Scan every segment of a way; return (dist_m, proj_lat, proj_lng, seg_bearing).

    *proj_lat / proj_lng* is the foot of the perpendicular dropped from
    (clat, clng) onto the nearest segment.  This is a much better estimate of
    where the street actually passes through the intersection than the nearest
    *node*, which may sit 10-20 m along an arm of the street.

    *seg_bearing* (0-180 undirected) is the bearing of that nearest segment —
    more accurate than the node-pair heuristic on curved streets.
    """
    best_dist = float("inf")
    best_lat, best_lng = clat, clng
    best_b = 0.0

    for i in range(len(geom) - 1):
        a, b = geom[i], geom[i + 1]
        proj_lat, proj_lng = _closest_point_on_segment(
            clat, clng, a["lat"], a["lon"], b["lat"], b["lon"]
        )
        d = _dist_m(clat, clng, proj_lat, proj_lng)
        if d < best_dist:
            best_dist = d
            best_lat, best_lng = proj_lat, proj_lng
            best_b = _bearing(a["lat"], a["lon"], b["lat"], b["lon"]) % 180

    return best_dist, best_lat, best_lng, best_b


# ── Geocoding ─────────────────────────────────────────────────────────────────

def _geocode_intersection_sync(query: str) -> dict:
    """Geocode a query; raise ValueError on failure."""
    params = {
        "address": query,
        "key": GOOGLE_API_KEY,
        "bounds": SEARCH_BOUNDS,
    }
    r = requests.get(GEOCODING_URL, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data["status"] != "OK" or not data.get("results"):
        raise ValueError(
            f"Could not find intersection '{query}': {data.get('status')}"
        )
    result = data["results"][0]
    loc    = result["geometry"]["location"]
    return {
        "formatted_address": result["formatted_address"],
        "lat": loc["lat"],
        "lng": loc["lng"],
    }


# ── OSM query ─────────────────────────────────────────────────────────────────

_SKIP_HIGHWAY_RE = re.compile(
    r"^(footway|cycleway|path|steps|pedestrian|track|construction|proposed)$"
)


_WAY_RADIUS_NORMAL  = 40   # metres — enough for most intersections
_WAY_RADIUS_WIDE    = 80   # metres — fallback when geocoder lands off-centre
                           # (e.g. Union Station plaza placing "16th & Wewatta"
                           # inside the concourse, 60–80 m from the street)


def _count_usable_street_names(ways: list[dict]) -> int:
    """Count distinct street names that survive the highway-type filter.

    Used to decide whether the way query returned enough streets to determine
    the intersection orientation without falling back to fixed NE/SE/SW/NW labels.
    """
    names: set[str] = set()
    for w in ways:
        hw = w.get("tags", {}).get("highway", "")
        if _SKIP_HIGHWAY_RE.match(hw):
            continue
        raw = w.get("tags", {}).get("name", "").strip()
        if not raw:
            continue
        names.add(_normalize_way_name(raw))
    return len(names)


def _run_way_query(radius: int, lat: float, lng: float) -> list[dict]:
    """Fire a single Overpass ways query and return raw elements."""
    way_q = f"""
[out:json][timeout:15];
way(around:{radius},{lat},{lng})["highway"]["name"];
out body geom;
"""
    try:
        r = requests.post(
            OVERPASS_URL, data={"data": way_q},
            headers=OVERPASS_HEADERS, timeout=20,
        )
        return r.json().get("elements", [])
    except Exception as e:
        print(f"[WARNING] OSM way query (intersection, r={radius}m): {e}")
        return []


def _osm_intersection_sync(lat: float, lng: float) -> dict:
    """Overpass calls for intersection signal nodes and named street ways.

    The signal-node query always uses a 40 m radius (signals are at or very
    near the kerb).  The way query starts at 40 m and retries at 80 m if
    fewer than two usable street names are found — this handles intersections
    where the geocoder places the result point inside a plaza or concourse
    (e.g. "16th & Wewatta St" lands inside Union Station, 60–80 m from where
    the streets actually cross).
    """
    node_radius = 40  # metres — signals are always close to the intersection

    node_q = f"""
[out:json][timeout:15];
(
  node(around:{node_radius},{lat},{lng})["highway"~"^(traffic_signals|stop|give_way|crossing)$"];
);
out body;
"""
    nodes: list[dict] = []
    ways:  list[dict] = []

    try:
        r1 = requests.post(
            OVERPASS_URL, data={"data": node_q},
            headers=OVERPASS_HEADERS, timeout=20,
        )
        nodes = r1.json().get("elements", [])
    except Exception as e:
        print(f"[WARNING] OSM node query (intersection): {e}")

    # Try narrow radius first; widen only if we can't identify two streets.
    ways  = _run_way_query(_WAY_RADIUS_NORMAL, lat, lng)
    n_streets = _count_usable_street_names(ways)
    if n_streets < 2:
        print(f"[INFO] Intersection way query: only {n_streets} street(s) at "
              f"{_WAY_RADIUS_NORMAL} m — retrying at {_WAY_RADIUS_WIDE} m")
        wide_ways = _run_way_query(_WAY_RADIUS_WIDE, lat, lng)
        if wide_ways:
            ways = wide_ways

    return {"nodes": nodes, "ways": ways}


# ── OSM result processing ─────────────────────────────────────────────────────

_MALL_SUFFIX_RE  = re.compile(r"\s+Mall$", re.IGNORECASE)
_DIR_PREFIX_RE   = re.compile(r"^(North|South|East|West)\s+", re.IGNORECASE)
_BEARING_MERGE_TOL = 20   # degrees; merge directional variants within this tolerance


def _base_street_name(name: str) -> str:
    """Strip a leading directional prefix for grouping purposes.

    "West 10th Ave" -> "10th Ave"
    "East Colfax Ave" -> "Colfax Ave"
    "Broadway" -> "Broadway"  (unchanged — no prefix)
    """
    return _DIR_PREFIX_RE.sub("", name)


def _normalize_way_name(name: str) -> str:
    """Collapse pedestrian-mall street names to their base street name.

    "16th Street Mall" -> "16th Street"

    In OSM, highway ways whose name ends in " Mall" are almost always the
    pedestrian section of a street that shares its name.  Stripping the suffix
    merges them with the regular street so they don't appear as a separate
    crossing direction at an intersection.
    """
    return _MALL_SUFFIX_RE.sub("", name).strip()


def _process_ways(
    ways: list[dict], clat: float, clng: float
) -> tuple[list[dict], tuple[float, float]]:
    """Extract unique named streets with their local compass orientation.

    Returns
    -------
    streets : list[dict]
        One entry per distinct street, sorted by the street's proximity to the
        intersection centre so that the two *main* streets (indices 0 and 1)
        are passed to ``_make_sorted_rays``.  Each dict has "name",
        "orientation", and "bearing".
    refined_center : (lat, lng)
        A more accurate intersection centre derived by projecting the geocoded
        centre perpendicularly onto each detected street's nearest segment and
        averaging the two projections.  This is geometrically guaranteed to be
        close to where the streets actually cross, regardless of how far the
        geocoder drifted from the carriageway.

    Implementation notes
    --------------------
    *Closest-segment* rule (replaces previous *closest-node* heuristic): for
    each named way we drop a perpendicular from the geocoded centre onto every
    segment and keep whichever projection is nearest.  That projection point
    and the segment bearing are more accurate than the bearing computed between
    the two closest nodes, especially for gently curved streets.

    A second pass collapses directional name variants that represent the same
    physical street.  At Broadway & 10th Ave (Denver's E/W dividing meridian)
    OSM records "West 10th Ave" and "East 10th Ave" as separate ways.  Because
    their undirected bearings are the same, they merge to "10th Ave".  Ways
    with the same base name but significantly different bearings are kept
    separate (they are genuinely distinct streets that share a name prefix).
    """
    # name -> (dist_to_centre_m, undirected_bearing_0_180, proj_lat, proj_lng)
    street_best: dict[str, tuple[float, float, float, float]] = {}

    for way in ways:
        tags    = way.get("tags", {})
        highway = tags.get("highway", "")
        if _SKIP_HIGHWAY_RE.match(highway):
            continue
        raw_name = tags.get("name", "").strip()
        if not raw_name:
            continue
        name = _normalize_way_name(raw_name)
        geom = way.get("geometry", [])
        if len(geom) < 2:
            continue

        # Project geocoded centre onto every segment; keep the nearest result.
        d, proj_lat, proj_lng, b = _closest_point_on_way(clat, clng, geom)

        prev = street_best.get(name)
        if prev is None or d < prev[0]:
            street_best[name] = (d, b, proj_lat, proj_lng)

    # ── Directional-variant merge pass ────────────────────────────────────────
    # Group full names by their base name (leading East/West/North/South stripped).
    base_groups: dict[str, list[tuple[str, float, float, float, float]]] = {}
    for full_name, (dist, bear, proj_lat, proj_lng) in street_best.items():
        base = _base_street_name(full_name)
        base_groups.setdefault(base, []).append((full_name, dist, bear, proj_lat, proj_lng))

    # display_name -> (dist, bearing, proj_lat, proj_lng)
    display: dict[str, tuple[float, float, float, float]] = {}
    for base, entries in base_groups.items():
        if len(entries) == 1:
            full_name, dist, bear, proj_lat, proj_lng = entries[0]
            display[full_name] = (dist, bear, proj_lat, proj_lng)
        else:
            # Check whether all variants are parallel (same undirected bearing).
            bears   = [e[2] for e in entries]
            b_range = max(bears) - min(bears)
            # Undirected bearings are 0–180; wraparound at 0/180 means the true
            # angular spread is min(b_range, 180 - b_range).
            spread = min(b_range, 180 - b_range)
            if spread <= _BEARING_MERGE_TOL:
                # Same physical street on both sides — merge to base name,
                # keep the projection of whichever arm is closest to the centre.
                best = min(entries, key=lambda e: e[1])
                _, dist, bear, proj_lat, proj_lng = best
                display[base] = (dist, bear, proj_lat, proj_lng)
            else:
                # Genuinely different streets sharing a name prefix; keep all.
                for full_name, dist, bear, proj_lat, proj_lng in entries:
                    display[full_name] = (dist, bear, proj_lat, proj_lng)

    # Sort by proximity so streets[0] and streets[1] are the two main streets.
    by_dist = sorted(display.items(), key=lambda kv: kv[1][0])

    # ── Refined intersection centre ───────────────────────────────────────────
    # Average the perpendicular projections of the two closest streets.
    # Geometrically this is very close to the actual road crossing even when
    # the geocoder returned a plaza centroid or a building entrance.
    if len(by_dist) >= 2:
        ref_lat = (by_dist[0][1][2] + by_dist[1][1][2]) / 2
        ref_lng = (by_dist[0][1][3] + by_dist[1][1][3]) / 2
    elif len(by_dist) == 1:
        ref_lat, ref_lng = by_dist[0][1][2], by_dist[0][1][3]
    else:
        ref_lat, ref_lng = clat, clng

    streets = [
        {
            "name":        name,
            "orientation": _street_orientation(vals[1]),
            "bearing":     round(vals[1], 1),
        }
        for name, vals in by_dist
    ]
    return streets, (ref_lat, ref_lng)


# Pairs: (OSM tag, expected value, human label)
_SIGNAL_FEATURE_TAGS: list[tuple[str, str, str]] = [
    ("button_operated",       "yes",  "pedestrian push button"),
    ("push_button",           "yes",  "pedestrian push button"),
    ("traffic_signals:sound", "yes",  "audible signal"),
    ("traffic_signals:sound", "walk", "audible signal"),
    ("tactile_paving",        "yes",  "tactile paving"),
    ("crossing:signals",      "yes",  "pedestrian signal phase"),
]


def _process_nodes(nodes: list[dict]) -> dict:
    """Summarise signal type and accessible features from OSM nodes."""
    all_tags: dict[str, str] = {}
    has_signals  = False
    has_stop     = False
    has_crossing = False

    for n in nodes:
        tags = n.get("tags", {})
        hw   = tags.get("highway", "")
        if hw == "traffic_signals":
            has_signals = True
        elif hw == "stop":
            has_stop = True
        elif hw in ("crossing", "give_way"):
            has_crossing = True
        all_tags.update(tags)

    if has_signals:
        signal_type = "traffic_signals"
    elif has_stop:
        signal_type = "stop_sign"
    elif has_crossing:
        signal_type = "marked_crossing"
    else:
        signal_type = "uncontrolled"

    features: list[str] = []
    seen: set[str] = set()
    for tag, expected, label in _SIGNAL_FEATURE_TAGS:
        if all_tags.get(tag) == expected and label not in seen:
            features.append(label)
            seen.add(label)

    return {"signal_type": signal_type, "features": features}


# ── Corner POI search ─────────────────────────────────────────────────────────

def _fetch_intersection_places_raw(lat: float, lng: float) -> list[dict]:
    """Fetch and filter Places near the intersection; return a flat list.

    Classification into corners is deferred so it can use the street bearings
    computed from the OSM result (which runs in parallel).
    """
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_API_KEY,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    body = {
        "locationRestriction": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": _INTERSECTION_SEARCH_RADIUS_M,
            }
        },
        "maxResultCount": 20,
    }
    try:
        r = requests.post(PLACES_V1_URL, json=body, headers=headers, timeout=12)
        r.raise_for_status()
        raw = r.json().get("places", [])
    except Exception as e:
        print(f"[WARNING] Intersection Places API: {e}")
        raw = []

    results: list[dict] = []
    seen_ids: set[str] = set()

    for place in raw:
        pid = place.get("id", "")
        if pid in seen_ids:
            continue
        if place.get("businessStatus") == "CLOSED_PERMANENTLY":
            continue

        loc  = place.get("location") or {}
        plat = loc.get("latitude")
        plng = loc.get("longitude")
        if plat is None or plng is None:
            continue

        types = set(place.get("types", []))
        if not _is_wanted(types):
            continue

        # Suppress bus gates and underground-only transit hubs — we are always
        # looking at an intersection from street level.
        place_for_filter = {"name": (place.get("displayName") or {}).get("text", ""), "types": list(types)}
        if is_underground_only(place_for_filter):
            continue

        name = place_for_filter["name"].strip()
        if not name:
            continue

        seen_ids.add(pid)
        results.append({
            "name":    name,
            "lat":     plat,
            "lng":     plng,
            "dist_m":  round(_dist_m(lat, lng, plat, plng)),
            "address": short_address(place.get("formattedAddress", "")),
        })

    return results


# ── Human-readable labels ─────────────────────────────────────────────────────

_SIGNAL_LABELS: dict[str, str] = {
    "traffic_signals": "Traffic signals",
    "stop_sign":       "Stop sign",
    "marked_crossing": "Marked crossing (no signal)",
    "uncontrolled":    "Uncontrolled (no signal or markings)",
}


# ── Async entry point ─────────────────────────────────────────────────────────

async def lookup_intersection(query: str) -> dict:
    """Geocode + OSM + Places, returning a structured orientation report."""
    normalized = normalize_intersection_query(query)
    full_query = _add_area_context(normalized)

    # Geocode first — lat/lng needed for the parallel lookups.
    geo = await asyncio.to_thread(_geocode_intersection_sync, full_query)
    lat, lng = geo["lat"], geo["lng"]

    # OSM and raw Places fetch in parallel.
    osm, raw_places = await asyncio.gather(
        asyncio.to_thread(_osm_intersection_sync, lat, lng),
        asyncio.to_thread(_fetch_intersection_places_raw, lat, lng),
    )

    # streets is sorted by proximity (closest street first) so _make_sorted_rays
    # always picks the two main streets rather than the first two alphabetically.
    streets_by_dist, refined_center = _process_ways(osm["ways"], lat, lng)
    ref_lat, ref_lng = refined_center

    signal      = _process_nodes(osm["nodes"])
    sorted_rays = _make_sorted_rays(streets_by_dist)
    corners     = _classify_corners(raw_places, ref_lat, ref_lng, sorted_rays)

    # Sort by name for the user-facing street list (stable, predictable order).
    streets = sorted(streets_by_dist, key=lambda s: s["name"])

    return {
        "intersection":    geo["formatted_address"],
        "lat":             lat,
        "lng":             lng,
        "streets":         streets,
        "signal_type":     signal["signal_type"],
        "signal_label":    _SIGNAL_LABELS.get(signal["signal_type"], signal["signal_type"]),
        "signal_features": signal["features"],
        "corners":         corners,
    }
