"""Test each step of get_route independently to find the hang."""
import asyncio, sys, time
sys.path.insert(0, ".")

async def main():
    from app.routing import fetch_directions, extract_route_geometry, build_direction_steps
    from app.crossings import build_route_linestring, find_crossings_along_route

    origin = "union station, Denver, CO"
    destination = "1630 chestnut pl, Denver, CO"

    print("1. fetch_directions ...", flush=True)
    t = time.time()
    data = await fetch_directions(origin, destination, "walking")
    print(f"   OK ({time.time()-t:.1f}s) — status: {data['routes'][0]['legs'][0]['distance']['text']}", flush=True)

    print("2. extract_route_geometry ...", flush=True)
    t = time.time()
    points, leg = extract_route_geometry(data)
    print(f"   OK ({time.time()-t:.1f}s) — {len(points)} polyline points", flush=True)

    print("3. build_route_linestring (Shapely) ...", flush=True)
    t = time.time()
    route_line = build_route_linestring(points)
    print(f"   OK ({time.time()-t:.1f}s)", flush=True)

    print("4. build_direction_steps ...", flush=True)
    t = time.time()
    steps = build_direction_steps(leg, route_line)
    print(f"   OK ({time.time()-t:.1f}s) — {len(steps)} direction steps", flush=True)

    print("5. find_crossings_along_route (Overpass + Shapely) ...", flush=True)
    t = time.time()
    crossings = await find_crossings_along_route(points)
    print(f"   OK ({time.time()-t:.1f}s) — {len(crossings)} crossings", flush=True)

asyncio.run(main())
