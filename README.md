# Denver Accessible Navigation

A locally-hosted navigation app that gives detailed, accessibility-focused
walking and transit directions across the Denver metro. Every street crossing,
driveway, alley, and parking-garage entrance along the route is detected and
listed as its own step — designed for orientation and mobility use.

This is **Phase 1**: routing + crossing detection. Phases 2–5 (personal
landmarks DB, Denver Open Data construction feed, RTD GTFS, Claude AI narrative
and follow-up chat) will be added next.

---

## What it does today

1. You enter a start and end address (Denver metro).
2. Backend geocodes both addresses via Google Maps.
3. Backend asks Google Directions for the walking or transit route.
4. Backend decodes the route polyline and queries OpenStreetMap (free) for
   every street, driveway, alley, parking aisle, and parking-garage entrance
   that crosses the route.
5. Frontend renders one ordered list interleaving Google's turn-by-turn
   directions with every detected crossing, color-coded and labeled.
6. A "Read all aloud" button uses your browser's speech synthesis.

---

## Setup (Windows, PowerShell)

### 1. Install Python dependencies

```powershell
cd C:\Users\User\denver-nav
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell blocks the activation script, run once (as your user):

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

### 2. Get a Google Maps API key

1. Go to https://console.cloud.google.com/
2. Create a new project (e.g. "denver-nav").
3. Enable these APIs:
   - **Geocoding API**
   - **Directions API**
4. Create an API key (APIs & Services → Credentials → Create credentials → API key).
5. (Recommended) Restrict the key to those two APIs.

You'll need a billing account on the project, but the **$200/month free
credit** covers personal use comfortably (Geocoding and Directions are both
$5/1000 calls).

### 3. Configure your key

```powershell
Copy-Item .env.example .env
notepad .env
```

Paste your key after `GOOGLE_API_KEY=`.

### 4. Run the server

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000 in any browser.

---

## Using it

- Type a starting address: e.g. `1275 E Colfax Ave, Denver`
- Type a destination: e.g. `Denver Union Station`
- Pick **Walking** or **Transit**
- Submit

You'll see a numbered list of steps. Step types:

| Tag | Meaning |
|---|---|
| **Direction** | Google's turn-by-turn step (e.g. "Head west on Colfax") |
| **Cross street** | A named street crossing your path |
| **Driveway** | Vehicle driveway crossing the sidewalk |
| **Alley** | Alley crossing your path |
| **Parking lot** | Parking-aisle crossing the sidewalk |
| **Garage entrance** | Tagged parking-garage entrance |
| **Service road** | Other service road |

"Read all aloud" speaks the full list via your browser's text-to-speech.

---

## Project layout

```
denver-nav/
├── app/
│   ├── main.py        FastAPI app, /api/geocode, /api/route, /
│   ├── models.py      Pydantic request/response models
│   ├── geocoding.py   Google Geocoding wrapper
│   ├── routing.py     Google Directions + step merging
│   └── crossings.py   OSM Overpass + Shapely crossing detection
├── static/
│   └── index.html     Accessible frontend (ARIA, keyboard, speech)
├── data/              (reserved for streets.json, landmarks.db)
├── requirements.txt
├── .env.example
└── README.md
```

---

## Coming next

- **Phase 2** — extract your `generate_guide.py` street data into `streets.json`,
  and a SQLite personal-landmarks DB with CRUD UI ("Add note here" on any step).
- **Phase 3** — Denver Open Data construction permits, RTD GTFS for transit
  stops with corner-of-intersection detection, OSM POI landmarks at waypoints.
- **Phase 4** — Claude API generates a single accessible narrative per route
  and powers a follow-up chat scoped to the current route.
- **Phase 5** — full ARIA polish, step-by-step "next step" mode, iPhone testing.
