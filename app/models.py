from pydantic import BaseModel, Field
from typing import Literal, Optional


class GeocodeRequest(BaseModel):
    address: str = Field(..., min_length=3)


class GeocodeResponse(BaseModel):
    formatted_address: str
    lat: float
    lng: float


class RouteRequest(BaseModel):
    origin: str = Field(..., min_length=3)
    destination: str = Field(..., min_length=3)
    mode: Literal["walking", "transit"] = "walking"


class Step(BaseModel):
    step_type: str
    instruction: str
    lat: float
    lng: float
    dist_along_route: float
    distance: Optional[str] = None
    duration: Optional[str] = None
    name: Optional[str] = None
    side: Optional[str] = None    # "left" | "right" — relative to direction of travel
    count: Optional[int] = None   # set when multiple identical steps are grouped
    address: Optional[str] = None # short street address for place steps


class RouteResponse(BaseModel):
    origin: str
    destination: str
    total_distance: str
    total_duration: str
    mode: str
    steps: list[Step]
    route_points: list[tuple[float, float]]


# ── Intersection lookup ───────────────────────────────────────────────────────

class IntersectionRequest(BaseModel):
    query: str = Field(..., min_length=5,
                       description="Intersection, e.g. '17th Ave & Wewatta St'")


class IntersectionStreet(BaseModel):
    name:        str
    orientation: str
    bearing:     float


class CornerPOI(BaseModel):
    name:    str
    dist_m:  int
    address: Optional[str] = None


class IntersectionResult(BaseModel):
    intersection:    str
    lat:             float
    lng:             float
    streets:         list[IntersectionStreet]
    signal_type:     str
    signal_label:    str
    signal_features: list[str]
    corners:         dict[str, list[CornerPOI]]


# ── Address description ───────────────────────────────────────────────────────

class DescribeRequest(BaseModel):
    address: str = Field(..., min_length=3)


class DescribedStreet(BaseModel):
    name:        str
    orientation: str
    bearing:     float
    dist_m:      int
    direction:   str


class DescribedPlace(BaseModel):
    name:      str
    address:   Optional[str] = None
    dist_m:    int
    direction: str
    lat:       float
    lng:       float
    types:     list[str] = []


class DescribeResult(BaseModel):
    address:   str
    lat:       float
    lng:       float
    streets:   list[DescribedStreet]
    streets_available: bool = True
    places:    list[DescribedPlace]
    narrative: list[str]
