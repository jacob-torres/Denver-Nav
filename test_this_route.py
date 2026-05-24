"""Diagnose the union station -> 1630 chestnut pl route step by step."""
import asyncio, sys, time
sys.path.insert(0, ".")

async def main():
    from app.geocoding import _add_area_context
    from app.routing import fetch_directions, extract_route_geometry
    from app.crossings import get_route_bbox, fetch_osm_ways, _shapely_intersections

    origin = _add_area_context("union station")
    dest   = _add_area_context("1630 chestnut pl")
    print(f"Expanded: {origin!r} -> {dest!r}")

    print("Fetching directions...", flush=True)
    data = await fetch_directions(origin, dest, "walking")
    leg = data["routes"][0]["legs"][0]
    print(f"  Distance: {leg['distance']['text']}, Duration: {leg['duration']['text']}")

    points, _ = extract_route_geometry(data)
    print(f"  Polyline points: {len(points)}")
    print(f"  First: {points[0]}, Last: {points[-1]}")

    bbox = get_route_bbox(points)
    print(f"  BBox: south={bbox[0]:.4f} west={bbox[1]:.4f} north={bbox[2]:.4f} east={bbox[3]:.4f}")
    area_km2 = (bbox[2]-bbox[0]) * (bbox[3]-bbox[1]) * 111 * 85
    print(f"  Approx bbox area: {area_km2:.2f} km²")

    print("Fetching OSM ways (timeout=60s)...", flush=True)
    t = time.time()
    elements = await fetch_osm_ways(bbox)
    print(f"  Got {len(elements)} elements in {time.time()-t:.1f}s")

    print("Running Shapely intersections...", flush=True)
    t = time.time()
    crossings = await asyncio.to_thread(_shapely_intersections, points, elements)
    print(f"  Got {len(crossings)} crossings in {time.time()-t:.1f}s")

asyncio.run(main())
