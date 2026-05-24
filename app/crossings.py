"""Detect every street crossing, driveway, alley, and parking entrance along a route.

Approach:
1. Build a Shapely LineString from the decoded Google polyline.
2. Query OSM Overpass API for all highway-tagged ways in the route's bbox.
3. For each way, test if it actually crosses the route (not just runs along it).
4. Classify and order crossings by position along the route.
"""

import asyncio
import math
import requests
from shapely.geometry import LineString, Point

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

OVERPASS_HEADERS = {
    "User-Agent": "DenverAccessibleNav/1.0 (accessibility/O&M project)",
    "Accept": "application/json, text/plain, */*",
}

# Approximate degrees-to-meters near Denver latitude (40 N).
METERS_PER_DEG = 95_000.0


def get_route_bbox(route_points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    """Return (south, west, north, east) bbox padded slightly."""
    lats = [p[0] for p in route_points]
    lngs = [p[1] for p in route_points]
    pad = 0.0008  # ~75 m padding
    return (min(lats) - pad, min(lngs) - pad, max(lats) + pad, max(lngs) + pad)


def build_route_linestring(route_points: list[tuple[float, float]]) -> LineString:
    """Shapely uses (x, y) = (lng, lat)."""
    return LineString([(lng, lat) for lat, lng in route_points])


def classify_crossing(highway: str, service: str, tags: dict) -> str | None:
    """Return a crossing_type label or None to skip."""
    if highway == "service":
        if service == "driveway":
            return "driveway"
        if service == "alley":
            return "alley"
        if service in ("parking_aisle", "parking"):
            return "parking"
        return "service_road"
    if highway in (
        "primary",
        "secondary",
        "tertiary",
        "residential",
        "unclassified",
        "living_street",
        "road",
        "tertiary_link",
        "secondary_link",
        "primary_link",
    ):
        return "street"
    return None


def format_crossing_instruction(name: str, crossing_type: str) -> str:
    name_part = name if name else ""
    if crossing_type == "street":
        return f"Cross {name_part}" if name_part else "Cross unnamed street"
    if crossing_type == "driveway":
        return "Driveway crossing" + (f" — {name_part}" if name_part else "")
    if crossing_type == "alley":
        return "Cross alley" + (f" — {name_part}" if name_part else "")
    if crossing_type == "parking":
        return "Parking lot crossing" + (f" — {name_part}" if name_part else "")
    if crossing_type == "parking_entrance":
        return "Parking garage entrance" + (f" — {name_part}" if name_part else "")
    if crossing_type == "service_road":
        return "Service road crossing" + (f" — {name_part}" if name_part else "")
    return f"Crossing — {name_part}" if name_part else "Crossing"


def _fetch_osm_ways_sync(bbox: tuple[float, float, float, float]) -> list[dict]:
    """Synchronous Overpass query — run in a thread via asyncio.to_thread."""
    south, west, north, east = bbox
    query = f"""
[out:json][timeout:45];
(
  way["highway"~"^(primary|secondary|tertiary|residential|service|unclassified|living_street|road|primary_link|secondary_link|tertiary_link)$"]({south},{west},{north},{east});
  node["amenity"="parking_entrance"]({south},{west},{north},{east});
);
out geom;
"""
    try:
        r = requests.post(
            OVERPASS_URL,
            data={"data": query},
            headers=OVERPASS_HEADERS,
            timeout=60,
        )
        r.raise_for_status()
        return r.json().get("elements", [])
    except Exception as e:
        print(f"[WARNING] Overpass API unavailable: {type(e).__name__}: {e}")
        return []


def compute_side(route_line: LineString, feature_point: Point) -> str | None:
    """Return 'left' or 'right' of the direction of travel at the nearest route point.

    Uses a 2-D cross product:  route_tangent × (nearest_route_pt → feature_pt).
    Positive cross product = LEFT,  negative = RIGHT.
    Returns None if the feature is so close to the route centreline that the
    direction is ambiguous (< 1 m lateral offset).
    """
    d = route_line.project(feature_point)
    segment_len = route_line.length

    # Sample tangent: points 5 m before and after the projection (in degree units).
    offset_deg = 5.0 / METERS_PER_DEG
    d_before = max(0.0, d - offset_deg)
    d_after = min(segment_len, d + offset_deg)
    if d_after <= d_before:
        return None

    pt_before = route_line.interpolate(d_before)
    pt_after = route_line.interpolate(d_after)

    # Route tangent vector (Shapely: x = lng, y = lat).
    tx = pt_after.x - pt_before.x
    ty = pt_after.y - pt_before.y

    # Lateral vector from nearest route point to feature.
    nearest = route_line.interpolate(d)
    dx = feature_point.x - nearest.x
    dy = feature_point.y - nearest.y

    # Reject if lateral offset is smaller than ~1 m (can't determine side reliably).
    lateral_m = math.hypot(dx, dy) * METERS_PER_DEG
    if lateral_m < 1.0:
        return None

    cross = tx * dy - ty * dx
    if abs(cross) < 1e-14:
        return None
    return "left" if cross > 0 else "right"


def _compute_side_from_way(route_line: LineString, way_line: LineString) -> str | None:
    """Determine which side of the route a way predominantly extends to.

    Samples 9 evenly-spaced points along the way and picks the one furthest
    from the route centreline — the sign of that cross product gives the side.
    """
    best_cross = 0.0
    offset_deg = 5.0 / METERS_PER_DEG
    seg_len = route_line.length

    for i in range(9):
        pt = way_line.interpolate(i / 8.0, normalized=True)
        d = route_line.project(pt)

        d_before = max(0.0, d - offset_deg)
        d_after = min(seg_len, d + offset_deg)
        if d_after <= d_before:
            continue

        pt_before = route_line.interpolate(d_before)
        pt_after = route_line.interpolate(d_after)
        tx = pt_after.x - pt_before.x
        ty = pt_after.y - pt_before.y

        nearest = route_line.interpolate(d)
        dx = pt.x - nearest.x
        dy = pt.y - nearest.y

        cross = tx * dy - ty * dx
        if abs(cross) > abs(best_cross):
            best_cross = cross

    if abs(best_cross) < 1e-14:
        return None
    # Require at least ~1.5 m lateral distance to call a side.
    best_lat_m = abs(best_cross) / (5.0 / METERS_PER_DEG) * METERS_PER_DEG
    if best_lat_m < 1.5:
        return None
    return "left" if best_cross > 0 else "right"


def _shapely_intersections(route_points: list[tuple[float, float]], elements: list[dict]) -> list[dict]:
    """CPU-bound Shapely work — combined with HTTP fetch in one thread call."""
    route_line = build_route_linestring(route_points)
    snap_tol_deg = 7.0 / METERS_PER_DEG
    crossings: list[dict] = []

    for el in elements:
        if el["type"] == "node":
            tags = el.get("tags", {})
            pt = Point(el["lon"], el["lat"])
            if route_line.distance(pt) > snap_tol_deg:
                continue
            dist_along = route_line.project(pt, normalized=True)
            side = compute_side(route_line, pt)
            crossings.append({
                "dist_along_route": dist_along,
                "lat": el["lat"],
                "lng": el["lon"],
                "name": tags.get("name", ""),
                "crossing_type": "parking_entrance",
                "side": side,
            })
            continue

        if el["type"] != "way" or "geometry" not in el:
            continue

        tags = el.get("tags", {})
        highway = tags.get("highway", "")
        service = tags.get("service", "")
        name = tags.get("name", "")
        crossing_type = classify_crossing(highway, service, tags)
        if crossing_type is None:
            continue

        coords = [(n["lon"], n["lat"]) for n in el["geometry"]]
        if len(coords) < 2:
            continue
        way_line = LineString(coords)

        try:
            inter = route_line.intersection(way_line)
        except Exception:
            continue
        if inter.is_empty:
            continue

        if inter.geom_type == "Point":
            cross_pt = inter
        elif inter.geom_type == "MultiPoint":
            geoms = list(inter.geoms)
            if not geoms:
                continue
            cross_pt = geoms[0]
        elif inter.geom_type == "GeometryCollection":
            pts = [g for g in inter.geoms if g.geom_type == "Point"]
            if not pts:
                continue
            cross_pt = pts[0]
        else:
            # LineString / MultiLineString => way runs along route, skip.
            continue

        dist_along = route_line.project(cross_pt, normalized=True)
        # For non-street crossings (driveways, alleys, etc.) determine which
        # side of the route the way predominantly extends to.
        if crossing_type == "street":
            side = None  # you cross the full width — side doesn't apply
        else:
            side = _compute_side_from_way(route_line, way_line)
        crossings.append({
            "dist_along_route": dist_along,
            "lat": cross_pt.y,
            "lng": cross_pt.x,
            "name": name,
            "crossing_type": crossing_type,
            "side": side,
        })

    crossings.sort(key=lambda c: c["dist_along_route"])
    return dedupe_crossings(crossings)


def _fetch_and_intersect(route_points: list[tuple[float, float]]) -> list[dict]:
    """Blocking: fetch OSM data then compute intersections. Run in a thread."""
    bbox = get_route_bbox(route_points)
    elements = _fetch_osm_ways_sync(bbox)
    return _shapely_intersections(route_points, elements)


async def find_crossings_along_route(
    route_points: list[tuple[float, float]],
) -> list[dict]:
    """Run all blocking work in a thread pool so the event loop stays free."""
    if len(route_points) < 2:
        return []
    return await asyncio.to_thread(_fetch_and_intersect, route_points)


def _dist_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    import math
    dlat = (lat2 - lat1) * 111_000
    dlng = (lng2 - lng1) * math.cos(math.radians((lat1 + lat2) / 2)) * 111_000
    return math.hypot(dlat, dlng)


def dedupe_crossings(crossings: list[dict]) -> list[dict]:
    """Collapse near-duplicate crossings (same name/type within 12 m of each other).

    Uses absolute meter distance so the threshold is route-length independent.
    12 m catches OSM geometry artifacts (the same way detected at two nearly
    identical coordinates) while preserving genuinely separate features.
    """
    if not crossings:
        return crossings
    DEDUP_M = 6.0
    deduped: list[dict] = []
    last_by_key: dict[str, dict] = {}
    for c in crossings:
        key = c["name"] or f"unnamed_{c['crossing_type']}"
        prev = last_by_key.get(key)
        if prev is not None and _dist_m(c["lat"], c["lng"], prev["lat"], prev["lng"]) < DEDUP_M:
            continue
        last_by_key[key] = c
        deduped.append(c)
    return deduped
