import traceback
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from app.models import (
    GeocodeRequest,
    GeocodeResponse,
    RouteRequest,
    RouteResponse,
    IntersectionRequest,
    IntersectionResult,
)
from app.config import SEARCH_AREA_NAME
from app.geocoding import geocode_address
from app.routing import get_route
from app.intersection import lookup_intersection

STATIC_DIR = Path(__file__).parent.parent / "static"

app = FastAPI(title="Denver Accessible Nav")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    tb = traceback.format_exc()
    print(f"\n[ERROR] Unhandled exception on {request.url}:\n{tb}")
    return JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {exc}", "traceback": tb},
    )


@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
async def config():
    return {"search_area_name": SEARCH_AREA_NAME}


@app.post("/api/geocode", response_model=GeocodeResponse)
async def geocode(req: GeocodeRequest):
    try:
        return await geocode_address(req.address)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/route", response_model=RouteResponse)
async def route(req: RouteRequest):
    try:
        return await get_route(req.origin, req.destination, req.mode)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        tb = traceback.format_exc()
        print(f"\n[ERROR] /api/route:\n{tb}", flush=True)
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.post("/api/intersection", response_model=IntersectionResult)
async def intersection(req: IntersectionRequest):
    try:
        return await lookup_intersection(req.query)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        tb = traceback.format_exc()
        print(f"\n[ERROR] /api/intersection:\n{tb}", flush=True)
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
