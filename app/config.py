"""Central config — reads from .env so the search area is easy to change later."""
import os
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv(usecwd=True))

GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "")

# Search area — edit these in .env to expand to a different city.
# SEARCH_AREA_NAME is appended to bare landmark searches ("Union Station" →
#   "Union Station, Denver, CO") so Google resolves to the right city.
# SEARCH_BOUNDS biases geocoding results to the metro bounding box
#   (format: "south,west|north,east").
SEARCH_AREA_NAME: str = os.getenv("SEARCH_AREA_NAME", "Denver, CO")
SEARCH_BOUNDS: str = os.getenv("SEARCH_BOUNDS", "39.50,-105.20|40.05,-104.45")
