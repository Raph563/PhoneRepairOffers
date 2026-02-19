from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.db.database import Database
from app.db.models import Offer, SearchRequest, ToggleFavoriteRequest
from app.services.favorites_service import FavoritesService
from app.services.search_service import SearchService

STARTED_AT = time.time()
APP_VERSION = os.environ.get("APP_VERSION", "0.1.0")
APP_PORT = int(os.environ.get("APP_PORT", "8091"))
DB_PATH = os.environ.get("DB_PATH", "/data/offers.db")
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "900"))

BASE_DIR = Path(__file__).resolve().parent

db = Database(DB_PATH)
search_service = SearchService(db=db, cache_ttl_seconds=CACHE_TTL_SECONDS)
favorites_service = FavoritesService(db=db)

app = FastAPI(title="PhoneRepairOffers", version=APP_VERSION)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        "index.html", {"request": request, "app_version": APP_VERSION}
    )


@app.get("/health")
def health():
    return {
        "ok": True,
        "version": APP_VERSION,
        "uptimeSeconds": int(time.time() - STARTED_AT),
    }


@app.post("/api/search")
def search(payload: SearchRequest):
    result = search_service.search(payload)
    return result


@app.get("/api/favorites")
def list_favorites(
    source: str | None = None, model: str | None = None, maxPriceEur: float | None = None
):
    payload = favorites_service.list_favorites()
    rows = payload["favorites"]

    if source:
        rows = [row for row in rows if str(row.get("offer", {}).get("source")) == source]

    if model:
        needle = model.strip().lower()
        rows = [row for row in rows if needle in str(row.get("offer", {}).get("title", "")).lower()]

    if maxPriceEur is not None:
        rows = [
            row
            for row in rows
            if float(row.get("offer", {}).get("totalEur", 0)) <= float(maxPriceEur)
        ]

    return {"ok": True, "favorites": rows}


@app.post("/api/favorites")
def create_favorite(payload: Offer):
    return favorites_service.create_favorite(payload)


@app.delete("/api/favorites/{favorite_id}")
def delete_favorite(favorite_id: int):
    result = favorites_service.delete_favorite(favorite_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail="Favorite not found")
    return {"ok": True}


@app.post("/api/favorites/toggle")
def toggle_favorite(payload: ToggleFavoriteRequest):
    result = favorites_service.toggle_favorite(payload)
    if not result.get("ok"):
        raise HTTPException(
            status_code=400, detail=result.get("error", "Unable to toggle favorite")
        )
    return result


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=APP_PORT, reload=False)
