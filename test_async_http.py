"""Test if httpx async GET works inside a basic asyncio loop."""
import asyncio, httpx, os, sys
sys.path.insert(0, ".")

async def main():
    print("Testing plain httpx GET to Google...")
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://www.google.com")
            print(f"  google.com: {r.status_code}")
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}")

    print("Testing Google Geocoding API...")
    from app.config import GOOGLE_API_KEY, SEARCH_AREA_NAME, SEARCH_BOUNDS
    print(f"  API key present: {bool(GOOGLE_API_KEY)}")
    print(f"  Area: {SEARCH_AREA_NAME}")
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={"address": "Union Station, Denver, CO", "key": GOOGLE_API_KEY, "bounds": SEARCH_BOUNDS}
            )
            data = r.json()
            print(f"  status: {data.get('status')}")
            if data.get("results"):
                print(f"  result: {data['results'][0]['formatted_address']}")
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}")

asyncio.run(main())
