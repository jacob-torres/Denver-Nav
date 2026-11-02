"""Offline checks for app.describe — Overpass and Places are mocked."""
from unittest import mock

from app import describe

# A north-south street (Chestnut Pl) ~10 m east of the point and an east-west
# one (16th St) ~30 m north.
LAT, LNG = 39.7530, -105.0000
WAYS = [
    {"tags": {"highway": "residential", "name": "Chestnut Pl"},
     "geometry": [{"lat": LAT - 0.001, "lon": LNG + 0.00012},
                  {"lat": LAT + 0.001, "lon": LNG + 0.00012}]},
    {"tags": {"highway": "tertiary", "name": "16th St"},
     "geometry": [{"lat": LAT + 0.00027, "lon": LNG - 0.001},
                  {"lat": LAT + 0.00027, "lon": LNG + 0.001}]},
    {"tags": {"highway": "footway", "name": "Some Footway"},
     "geometry": [{"lat": LAT, "lon": LNG}, {"lat": LAT, "lon": LNG + 0.0001}]},
]


def _place(pid, name, addr, dlat, dlng, types=("restaurant",), status=None):
    p = {"id": pid, "displayName": {"text": name}, "formattedAddress": addr,
         "location": {"latitude": LAT + dlat, "longitude": LNG + dlng},
         "types": list(types)}
    if status:
        p["businessStatus"] = status
    return p


PLACES = [
    _place("a", "Cafe One", "1630 Chestnut Pl #120, Denver, CO", 0.0, 0.0001, ("cafe",)),
    _place("b", "Bank Two", "1632 Chestnut Pl Ste 200, Denver, CO", 0.0004, 0.0001, ("bank",)),
    _place("c", "Gone Diner", "1600 Chestnut Pl, Denver, CO", 0.0001, 0.0001,
           status="CLOSED_PERMANENTLY"),
    _place("d", "Far Shop", "9 Faraway Rd, Denver, CO", 0.01, 0.0, ("book_store",)),
    _place("e", "Airbnb", "5 Elm St, Denver, CO", 0.0002, 0.0, ("lodging",)),
    _place("f", "Stevens Deli", "1700 Stevens St, Denver, CO", -0.0003, 0.0003, ("cafe",)),
]

GEO = {"formatted_address": "1630 Chestnut Pl, Denver, CO 80202, USA",
       "lat": LAT, "lng": LNG}


def _run(ways):
    with mock.patch.object(describe, "_geocode_sync", return_value=GEO), \
         mock.patch.object(describe, "_query_ways", return_value=ways), \
         mock.patch.object(describe, "_nearby_search", return_value=PLACES):
        return describe._describe_sync("1630 Chestnut Pl")


def test_street_of_strips_units_but_not_names():
    assert describe._street_of("1630 Chestnut Pl #120") == "Chestnut Pl"
    assert describe._street_of("2001 16th St Ste 1700") == "16th St"
    assert describe._street_of("1920 17th St Suite B") == "17th St"
    assert describe._street_of("1700 Stevens St") == "Stevens St"


def test_full_description():
    r = _run(WAYS)
    assert r["streets_available"]
    assert [s["name"] for s in r["streets"]] == ["Chestnut Pl", "16th St"]
    assert r["streets"][0]["orientation"] == "runs north-south"
    assert sorted(p["name"] for p in r["places"]) == ["Bank Two", "Cafe One", "Stevens Deli"]
    text = "\n".join(r["narrative"])
    assert "Gone Diner" not in text and "Far Shop" not in text and "Airbnb" not in text
    assert "On Chestnut Pl:" in text and "On Stevens St:" in text
    # 1630 is even on a N-S street -> east side.
    assert r["narrative"][0].startswith("1630 is on the east side of Chestnut Pl"), r["narrative"][0]


def test_overpass_outage_is_reported():
    r = _run(None)
    assert not r["streets_available"]
    assert "temporarily unavailable" in r["narrative"][0]
    assert any("Cafe One" in line for line in r["narrative"])


if __name__ == "__main__":
    test_street_of_strips_units_but_not_names()
    test_full_description()
    test_overpass_outage_is_reported()
    print("all describe tests passed")
