"""Denver metropolitan address system rules.

Source: Greater Metropolitan Denver Street Guide (Colorado DVR, 2005).

Key rules:
  NOW  = North, Odd, West   — odd-numbered addresses are on N and W sides
  SEE  = South, Even, East  — even-numbered addresses are on S and E sides

Downtown diagonal exception (streets run at ~45° to the compass):
  15th-St parallels (run NW-SE): odd = NE side, even = SW side
  Larimer parallels (run NE-SW): odd = NW side, even = SE side

Last two digits of an address = approximate % of the block
from the lower-numbered cross street toward the next.
  e.g. 1630 → in the 1600 block, 30 % of the way from 16th toward 17th.
"""

import math
import re


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_address_number(address: str) -> int | None:
    """Extract the leading street number from an address string."""
    m = re.match(r"^\s*(\d+)", address.strip())
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Approach bearing from route geometry
# ---------------------------------------------------------------------------

def approach_bearing(route_points: list[tuple[float, float]]) -> float | None:
    """Return the compass bearing (0 = N, 90 = E, …) of the final approach.

    Walks backward through route_points to find the last segment that is at
    least 3 m long (avoids noisy single-point jitter at the destination).
    """
    for i in range(len(route_points) - 1, 0, -1):
        lat1, lng1 = route_points[i - 1]
        lat2, lng2 = route_points[i]
        dlat_m = (lat2 - lat1) * 111_000
        dlng_m = (lng2 - lng1) * math.cos(math.radians((lat1 + lat2) / 2)) * 111_000
        if math.hypot(dlat_m, dlng_m) >= 3.0:
            bearing = math.degrees(math.atan2(dlng_m, dlat_m))
            return (bearing + 360) % 360
    return None


# ---------------------------------------------------------------------------
# Street axis classification
# ---------------------------------------------------------------------------

def street_axis(bearing: float) -> str:
    """Classify street orientation from the approach bearing.

    Returns one of: 'NS', 'EW', 'NESW' (Larimer-parallel diagonal),
    'NWSE' (15th-St-parallel diagonal).
    """
    b = bearing % 180  # collapse N-S and E-W to a single 0–180 range
    if b < 22.5 or b >= 157.5:
        return "NS"
    elif b < 67.5:
        return "NESW"   # ~45° — Larimer / Chestnut / Wewatta direction
    elif b < 112.5:
        return "EW"
    else:
        return "NWSE"   # ~135° — 15th St / 16th St / Welton direction


# ---------------------------------------------------------------------------
# Geographic side determination (NOW / SEE + downtown exceptions)
# ---------------------------------------------------------------------------

def address_geographic_side(number: int, axis: str) -> str:
    """Return the geographic side ('N', 'S', 'E', 'W', 'NE', 'SE', 'NW', 'SW')
    for the given house number and street axis.

    Standard grid  (NOW / SEE):
      NS streets:   odd → W,  even → E
      EW avenues:   odd → N,  even → S
    Downtown diagonals:
      NESW (Larimer parallel):  odd → NW, even → SE
      NWSE (15th-St parallel):  odd → NE, even → SW
    """
    even = (number % 2 == 0)
    rules: dict[str, tuple[str, str]] = {
        "NS":   ("W",  "E"),
        "EW":   ("N",  "S"),
        "NESW": ("NW", "SE"),
        "NWSE": ("NE", "SW"),
    }
    odd_side, even_side = rules[axis]
    return even_side if even else odd_side


# ---------------------------------------------------------------------------
# Left / right relative to direction of travel
# ---------------------------------------------------------------------------

_GEO_BEARING: dict[str, float] = {
    "N": 0, "NE": 45, "E": 90, "SE": 135,
    "S": 180, "SW": 225, "W": 270, "NW": 315,
}

_GEO_READABLE: dict[str, str] = {
    "N": "north", "NE": "northeast", "E": "east", "SE": "southeast",
    "S": "south", "SW": "southwest", "W": "west", "NW": "northwest",
}


def _angular_diff(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return min(d, 360 - d)


def side_relative_to_bearing(bearing: float, geo_side: str) -> str:
    """Return 'left' or 'right' of the walking direction for a geographic side."""
    geo_b = _GEO_BEARING[geo_side]
    left_b = (bearing - 90) % 360
    right_b = (bearing + 90) % 360
    if _angular_diff(geo_b, left_b) <= _angular_diff(geo_b, right_b):
        return "left"
    return "right"


# ---------------------------------------------------------------------------
# Block-position description
# ---------------------------------------------------------------------------

def block_position_text(number: int) -> str:
    """Describe how far into the block the address falls.

    Uses the last-two-digits rule: e.g. 1630 → 30 % into the 1600 block.
    When the block start is a round hundred and ≤ 99 (numbered cross streets),
    names the bounding cross streets.
    """
    pct = number % 100
    block_start = (number // 100) * 100
    block_num = block_start // 100         # e.g. 16 for the 1600 block

    # Readable fraction
    if pct == 0:
        frac = "at the very start"
    elif pct <= 20:
        frac = "near the start"
    elif pct <= 40:
        frac = f"about {pct}% of the way"
    elif pct <= 60:
        frac = "about midway"
    elif pct <= 80:
        frac = f"about {pct}% of the way"
    else:
        frac = "near the far end"

    if block_start > 0 and block_num <= 99:
        # Numbered cross streets can be inferred.
        return (
            f"{frac} into the {block_start} block "
            f"(between {block_num}th and {block_num + 1}th)"
        )
    return f"{frac} into the block"


# ---------------------------------------------------------------------------
# Top-level: build a destination-arrival instruction
# ---------------------------------------------------------------------------

def destination_arrival_text(destination: str, route_points: list[tuple[float, float]]) -> str:
    """Compose a human-readable arrival instruction using Denver address rules.

    Returns a string such as:
      "Arrive at 1630 Chestnut Pl — on the left (southeast) side,
       about 30 % of the way into the 1600 block (between 16th and 17th)"
    """
    number = parse_address_number(destination)
    if number is None:
        return f"Arrive at destination: {destination}"

    bearing = approach_bearing(route_points)
    if bearing is None:
        return f"Arrive at {destination}"

    axis = street_axis(bearing)
    geo_side = address_geographic_side(number, axis)
    lr = side_relative_to_bearing(bearing, geo_side)
    readable = _GEO_READABLE[geo_side]
    block_text = block_position_text(number)

    return (
        f"Arrive at {destination} — "
        f"on the {lr} ({readable}) side of the street, "
        f"{block_text}"
    )
