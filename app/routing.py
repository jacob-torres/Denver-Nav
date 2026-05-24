"""Get a Google Directions route, decode its polyline, and merge in every
street crossing, driveway, alley, and parking entrance detected from OSM.
"""

import asyncio
import math
import re
import html
import requests
import polyline
from shapely.geometry import Point

from app.config import GOOGLE_API_KEY
from app.geocoding import _add_area_context, normalize_intersection_query
from app.crossings import (
    build_route_linestring,
    find_crossings_along_route,
    format_crossing_instruction,
)
from app.address_rules import destination_arrival_text
from app.places import find_places_along_route, is_underground_only


def clean_instruction(text: str) -> str:
    """Strip Google's HTML and 'Destination will be on the…' from instructions."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    # Remove Google's side-of-street claim — we replace it with our own step.
    text = re.sub(r"\s*Destination will be on the (?:left|right)\.?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _fetch_directions_sync(origin: str, destination: str, mode: str) -> dict:
    if not GOOGLE_API_KEY:
        raise RuntimeError("GOOGLE_API_KEY not set in .env")

    # Normalize intersection syntax ("A and B" / "A / B" → "A & B") so that
    # Google Directions handles them as intersection queries, not street names.
    origin      = normalize_intersection_query(origin)
    destination = normalize_intersection_query(destination)

    params = {
        "origin": origin,
        "destination": destination,
        "mode": mode,
        "key": GOOGLE_API_KEY,
        "region": "us",
        "units": "imperial",
    }
    if mode == "transit":
        params["transit_mode"] = "bus|rail|tram|subway"

    r = requests.get(
        "https://maps.googleapis.com/maps/api/directions/json",
        params=params,
        timeout=20,
    )
    data = r.json()

    if data["status"] != "OK" or not data.get("routes"):
        raise ValueError(
            f"Directions failed: {data.get('status')} — {data.get('error_message', '')}"
        )
    return data


async def fetch_directions(origin: str, destination: str, mode: str) -> dict:
    return await asyncio.to_thread(_fetch_directions_sync, origin, destination, mode)


def extract_route_geometry(google_data: dict) -> tuple[list[tuple[float, float]], dict]:
    """Use the per-step polylines (more detailed than overview_polyline)."""
    leg = google_data["routes"][0]["legs"][0]
    points: list[tuple[float, float]] = []
    for step in leg["steps"]:
        encoded = step.get("polyline", {}).get("points")
        if encoded:
            decoded = polyline.decode(encoded)
            if points and decoded and points[-1] == decoded[0]:
                points.extend(decoded[1:])
            else:
                points.extend(decoded)
    if not points:
        points = polyline.decode(google_data["routes"][0]["overview_polyline"]["points"])
    return points, leg


def build_direction_steps(
    leg: dict, route_line
) -> tuple[list[dict], list[tuple[float, float]]]:
    """Parse leg steps into direction/transit steps.

    Returns ``(steps, transit_ranges)`` where *transit_ranges* is a list of
    ``(board_dist, exit_dist)`` normalized floats (0–1) for each TRANSIT
    segment.  Callers use *transit_ranges* to filter out crossings and places
    that fall inside a transit ride.
    """
    steps: list[dict] = []
    transit_ranges: list[tuple[float, float]] = []

    for s in leg["steps"]:
        start = s["start_location"]

        if s.get("travel_mode") == "TRANSIT":
            td = s.get("transit_details") or {}
            line_obj = td.get("line") or {}
            vehicle = line_obj.get("vehicle") or {}

            line_name = line_obj.get("short_name") or line_obj.get("name", "")
            vehicle_name = vehicle.get("name") or "Transit"
            headsign = td.get("headsign", "")

            dep_stop = (td.get("departure_stop") or {}).get("name", "")
            arr_stop = (td.get("arrival_stop") or {}).get("name", "")
            dep_time = (td.get("departure_time") or {}).get("text", "")
            arr_time = (td.get("arrival_time") or {}).get("text", "")
            num_stops = td.get("num_stops")

            # ── Board step ────────────────────────────────────────────────────
            board_parts = [vehicle_name]
            if line_name:
                board_parts.append(f"Line {line_name}")
            if headsign:
                board_parts.append(f"toward {headsign}")
            board_instr = "Board " + " ".join(board_parts)
            if dep_stop:
                board_instr += f" at {dep_stop}"
            if dep_time:
                board_instr += f" — departing {dep_time}"

            # ── Exit step ─────────────────────────────────────────────────────
            exit_instr = f"Exit at {arr_stop}" if arr_stop else "Exit transit"
            exit_extras: list[str] = []
            if arr_time:
                exit_extras.append(f"arriving {arr_time}")
            if num_stops:
                s_label = "stop" if num_stops == 1 else "stops"
                exit_extras.append(f"{num_stops} {s_label}")
            if exit_extras:
                exit_instr += " — " + ", ".join(exit_extras)

            # ── Positions ─────────────────────────────────────────────────────
            end = s["end_location"]
            start_pt = Point(start["lng"], start["lat"])
            end_pt   = Point(end["lng"],   end["lat"])
            board_dist = route_line.project(start_pt, normalized=True)
            exit_dist  = route_line.project(end_pt,   normalized=True)

            steps.append({
                "step_type": "transit_board",
                "instruction": board_instr,
                "lat": start["lat"],
                "lng": start["lng"],
                "dist_along_route": board_dist,
                "distance": s.get("distance", {}).get("text"),
                "duration": s.get("duration", {}).get("text"),
                "name": line_name or None,
            })
            steps.append({
                "step_type": "transit_exit",
                "instruction": exit_instr,
                "lat": end["lat"],
                "lng": end["lng"],
                "dist_along_route": exit_dist,
                "distance": None,
                "duration": None,
                "name": arr_stop or None,
            })
            transit_ranges.append((board_dist, exit_dist))

        else:
            instruction = clean_instruction(s.get("html_instructions", ""))
            pt = Point(start["lng"], start["lat"])
            dist = route_line.project(pt, normalized=True)
            steps.append({
                "step_type": "direction",
                "instruction": instruction,
                "lat": start["lat"],
                "lng": start["lng"],
                "dist_along_route": dist,
                "distance": s.get("distance", {}).get("text"),
                "duration": s.get("duration", {}).get("text"),
                "name": None,
            })

    return steps, transit_ranges


def _dist_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    dlat = (lat2 - lat1) * 111_000
    dlng = (lng2 - lng1) * math.cos(math.radians((lat1 + lat2) / 2)) * 111_000
    return math.hypot(dlat, dlng)


_SUFFIX_WORDS: set[str] = {
    "street", "avenue", "boulevard", "place", "court", "drive",
    "way", "lane", "road", "parkway", "trail", "circle", "terrace",
    "ave", "st", "blvd", "pl", "ct", "dr", "ln", "rd", "pkwy",
    "north", "south", "east", "west", "n", "s", "e", "w",
}


def _street_name_in_instruction(crossing_name: str, instruction: str) -> bool:
    """Return True if a meaningful word from crossing_name appears in instruction."""
    if not crossing_name:
        return False
    words = [
        w.strip(".,;:")
        for w in crossing_name.lower().split()
        if len(w.strip(".,;:")) >= 3
        and w.strip(".,;:").lower() not in _SUFFIX_WORDS
    ]
    if not words:
        return False
    instr_lower = instruction.lower()
    return any(w in instr_lower for w in words)


def merge_and_dedupe(direction_steps: list[dict], crossings: list[dict]) -> list[dict]:
    """Combine into one ordered list, suppressing crossings that coincide with turns.

    A crossing is suppressed only when it is BOTH within NEAR_TURN_M of a
    direction step AND the crossing street name appears in that step's
    instruction.  This preserves "Cross Wewatta" even when close to a step
    that says "Continue on 17th St", while still suppressing "Cross Chestnut"
    adjacent to "Turn left onto Chestnut Pl".
    """
    NEAR_TURN_M = 15.0     # reduced radius — name matching does the precision work
    NEAR_PARKING_M = 20.0  # suppress service_road within 20 m of a parking_entrance

    direction_data = [(s["lat"], s["lng"], s["instruction"]) for s in direction_steps]

    out: list[dict] = list(direction_steps)
    for c in crossings:
        crossing_name = c.get("name", "")
        if any(
            _dist_m(c["lat"], c["lng"], dlat, dlng) < NEAR_TURN_M
            and _street_name_in_instruction(crossing_name, dinstr)
            for dlat, dlng, dinstr in direction_data
        ):
            continue
        base_instruction = format_crossing_instruction(
            c.get("name", ""), c["crossing_type"]
        )
        side = c.get("side")
        instruction = (
            f"{base_instruction} — on the {side}" if side else base_instruction
        )
        out.append(
            {
                "step_type": c["crossing_type"],
                "instruction": instruction,
                "lat": c["lat"],
                "lng": c["lng"],
                "dist_along_route": c["dist_along_route"],
                "distance": None,
                "duration": None,
                "name": c.get("name") or None,
                "side": side,
            }
        )
    out.sort(key=lambda s: s["dist_along_route"])

    # Collapse duplicate named street crossings within 30 m of each other.
    # This handles the case where two OSM way segments for the same street
    # both intersect the route polyline within the dedupe_crossings 6 m radius.
    NEAR_SAME_CROSSING_M = 30.0
    seen_named: list[dict] = []
    deduped_out: list[dict] = []
    for step in out:
        if step["step_type"] == "street" and step.get("name"):
            already = next(
                (
                    s for s in seen_named
                    if s["name"] == step["name"]
                    and _dist_m(s["lat"], s["lng"], step["lat"], step["lng"]) < NEAR_SAME_CROSSING_M
                ),
                None,
            )
            if already:
                continue
            seen_named.append(step)
        deduped_out.append(step)
    out = deduped_out

    # Suppress service_road crossings co-located with a parking_entrance.
    parking_locs = [(s["lat"], s["lng"]) for s in out if s["step_type"] == "parking_entrance"]
    out = [
        s for s in out
        if not (
            s["step_type"] == "service_road"
            and any(_dist_m(s["lat"], s["lng"], plat, plng) < NEAR_PARKING_M
                    for plat, plng in parking_locs)
        )
    ]

    return out




def insert_places(
    steps: list[dict],
    places: list[dict],
    has_street_crossings: bool = False,
) -> list[dict]:
    """Merge place POIs into the step list and re-sort by position.

    Bus terminal gates are suppressed on above-ground routes, detected via
    `has_street_crossings` (passed from raw OSM crossings before any merge-
    suppression, so gates disappear even when the crossing itself is hidden).
    Underground concourse routes have zero street crossings and keep the gates.
    """
    above_ground = has_street_crossings

    for p in places:
        if above_ground and is_underground_only(p):
            continue
        side = p.get("side")
        name = p["name"]
        addr = p.get("vicinity", "")
        instruction = f"{name} — on the {side}" if side else name
        steps.append({
            "step_type": "place",
            "instruction": instruction,
            "lat": p["lat"],
            "lng": p["lng"],
            "dist_along_route": p["dist_along_route"],
            "distance": None,
            "duration": None,
            "name": name,
            "side": side,
            "address": addr or None,
        })
    steps.sort(key=lambda s: s["dist_along_route"])
    return steps


_GROUPABLE_TYPES: dict[str, tuple[str, str]] = {
    # step_type: (singular label, plural label)
    "parking_entrance": ("parking garage entrance", "parking garage entrances"),
    "driveway":         ("driveway",                "driveways"),
    "alley":            ("alley crossing",           "alley crossings"),
    "parking":          ("parking lot crossing",     "parking lot crossings"),
    "service_road":     ("service road crossing",    "service road crossings"),
}


def group_consecutive_steps(steps: list[dict]) -> list[dict]:
    """Merge consecutive same-type crossing steps into a single counted step.

    Two consecutive steps are merged when they share the same step_type.
    The side of the grouped step reflects whether all members share one side
    ("on the left / right") or are split ("on both sides").
    """
    result: list[dict] = []
    i = 0
    while i < len(steps):
        step = steps[i]
        type_ = step["step_type"]

        if type_ not in _GROUPABLE_TYPES:
            result.append(step)
            i += 1
            continue

        # Collect run of consecutive same-type steps.
        run = [step]
        j = i + 1
        while j < len(steps) and steps[j]["step_type"] == type_:
            run.append(steps[j])
            j += 1

        if len(run) == 1:
            result.append(step)
            i = j
            continue

        # Build the grouped step.
        n = len(run)
        singular, plural = _GROUPABLE_TYPES[type_]
        sides = {s["side"] for s in run if s.get("side")}

        if len(sides) == 1:
            side_text = f" — on the {sides.pop()}"
            group_side = list(sides)[0] if sides else None  # re-read before pop consumed it
        elif len(sides) == 2:
            side_text = " — on both sides"
            group_side = None
        else:
            side_text = ""
            group_side = None

        # Re-derive side after pop (pop above emptied the set, safe to re-check run)
        all_sides = [s["side"] for s in run if s.get("side")]
        unique_sides = set(all_sides)
        if len(unique_sides) == 1:
            group_side = all_sides[0]
        else:
            group_side = None

        grouped = dict(run[0])
        grouped["instruction"] = f"{n} {plural}{side_text}"
        grouped["count"] = n
        grouped["side"] = group_side
        result.append(grouped)
        i = j

    return result


# Matches direction instructions that represent a genuine change of street.
_TURN_RE = re.compile(
    r"\b(turn|head|depart|merge|keep|bear|slight|continue onto)\b", re.IGNORECASE
)


def _extract_street_name(instruction: str) -> str | None:
    """Pull the street name from a Google direction instruction.

    Handles:
      "Turn left onto Chestnut Pl"         → "Chestnut Pl"
      "Continue on 17th St"                → "17th St"
      "Head northwest on 17th St toward …" → "17th St"
    """
    m = re.search(
        r"\b(?:onto|on|along)\s+([A-Za-z0-9][A-Za-z0-9 \-\.]+?)(?:\s+toward\b|\s*$)",
        instruction,
        re.IGNORECASE,
    )
    return m.group(1).strip() if m else None


def insert_block_headers(steps: list[dict]) -> list[dict]:
    """Insert block_header steps to group crossings and places by city block.

    A block_header is emitted:
      • After each turn direction step (e.g. "Turn left onto Chestnut Pl")
      • After each named street crossing (you've just entered a new block)

    Headers are only inserted when there is at least one place-like step
    (place, parking_entrance, driveway, alley, etc.) in the block that
    follows, so empty sections never appear.

    Individual place instructions are left clean (just name + side);
    the block context lives solely in the header above.
    """
    # Step types that benefit from a block heading above them.
    _PLACE_LIKE = {
        "place", "parking_entrance", "driveway",
        "alley", "parking", "service_road",
    }

    def _has_content(start: int, end: int) -> bool:
        return any(steps[j]["step_type"] in _PLACE_LIKE for j in range(start, end))

    def _next_boundary(from_idx: int) -> int:
        """Index of the next block boundary (direction step or named crossing)."""
        for j in range(from_idx + 1, len(steps)):
            if steps[j]["step_type"] == "direction":
                return j
            if steps[j]["step_type"] == "street" and steps[j].get("name"):
                return j
        return len(steps)

    def _to_street_ahead(from_idx: int) -> str | None:
        """First block boundary name after from_idx (named crossing or next turn)."""
        for j in range(from_idx + 1, len(steps)):
            s = steps[j]
            if s["step_type"] == "street" and s.get("name"):
                return s["name"]
            if s["step_type"] == "direction" and _TURN_RE.search(s["instruction"]):
                return _extract_street_name(s["instruction"])
        return None

    def _make_label(current: str | None, frm: str | None, to: str | None) -> str | None:
        if not current:
            return None
        if frm and to and frm != to:
            return f"On {current} between {frm} and {to}"
        if frm:
            return f"On {current} past {frm}"
        if to:
            return f"On {current} before {to}"
        return f"On {current}"

    def _header_step(label: str, anchor: dict) -> dict:
        return {
            "step_type": "block_header",
            "instruction": label,
            "lat": anchor["lat"],
            "lng": anchor["lng"],
            "dist_along_route": anchor["dist_along_route"],
            "distance": None,
            "duration": None,
            "name": None,
        }

    result: list[dict] = []
    current_street: str | None = None
    last_dir: dict | None = None        # most recent direction step seen
    last_header_label: str | None = None

    for i, step in enumerate(steps):
        result.append(step)

        emit = False
        from_street: str | None = None

        if step["step_type"] == "direction":
            new_street = _extract_street_name(step["instruction"])
            is_turn = bool(_TURN_RE.search(step["instruction"]))

            if is_turn:
                # "From" = street we were just walking along.
                from_street = (
                    _extract_street_name(last_dir["instruction"]) if last_dir else None
                )
                emit = True
            else:
                # "Continue on X" waypoint — update current street but don't
                # emit a header; the crossing-based header handles sub-blocks.
                prev_cross = next(
                    (steps[j] for j in range(i - 1, -1, -1)
                     if steps[j]["step_type"] == "street" and steps[j].get("name")),
                    None,
                )
                from_street = prev_cross["name"] if prev_cross else None
                # Only emit if the street name actually changed.
                emit = (new_street != current_street) if new_street else False

            current_street = new_street
            last_dir = step

        elif step["step_type"] == "street" and step.get("name"):
            from_street = step["name"]
            emit = True

        if emit:
            to_street = _to_street_ahead(i)
            label = _make_label(current_street, from_street, to_street)
            next_bdry = _next_boundary(i)
            if (
                label
                and label != last_header_label
                and _has_content(i + 1, next_bdry)
            ):
                result.append(_header_step(label, step))
                last_header_label = label

    return result


def _in_transit(dist: float, transit_ranges: list[tuple[float, float]]) -> bool:
    """Return True if *dist* (normalized 0–1) falls inside any transit segment."""
    return any(lo <= dist <= hi for lo, hi in transit_ranges)


async def get_route(origin: str, destination: str, mode: str) -> dict:
    google_data = await fetch_directions(
        _add_area_context(origin), _add_area_context(destination), mode
    )
    route_points, leg = extract_route_geometry(google_data)
    route_line = build_route_linestring(route_points)

    direction_steps, transit_ranges = build_direction_steps(leg, route_line)

    # Fetch OSM crossings and Google Places in parallel.
    crossings, places = await asyncio.gather(
        find_crossings_along_route(route_points),
        find_places_along_route(route_points),
    )

    # Detect above-ground before any merge suppression removes street crossings.
    has_street_crossings = any(c["crossing_type"] == "street" for c in crossings)

    # Drop crossings and places that fall inside a bus/train ride — those
    # segments get transit_board / transit_exit steps instead.
    if transit_ranges:
        crossings = [c for c in crossings
                     if not _in_transit(c["dist_along_route"], transit_ranges)]
        places    = [p for p in places
                     if not _in_transit(p["dist_along_route"], transit_ranges)]

    steps = merge_and_dedupe(direction_steps, crossings)
    steps = insert_places(steps, places, has_street_crossings=has_street_crossings)
    steps = group_consecutive_steps(steps)
    steps = insert_block_headers(steps)

    # Append a destination-arrival step at the very end, computed from Denver
    # address system rules (NOW/SEE + downtown diagonal exceptions).
    if route_points:
        dest_lat, dest_lng = route_points[-1]
        steps.append({
            "step_type": "destination",
            "instruction": destination_arrival_text(destination, route_points),
            "lat": dest_lat,
            "lng": dest_lng,
            "dist_along_route": 1.0,
            "distance": None,
            "duration": None,
            "name": None,
        })

    return {
        "origin": leg.get("start_address", origin),
        "destination": leg.get("end_address", destination),
        "total_distance": leg.get("distance", {}).get("text", ""),
        "total_duration": leg.get("duration", {}).get("text", ""),
        "mode": mode,
        "steps": steps,
        "route_points": route_points,
    }
