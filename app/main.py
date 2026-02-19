from __future__ import annotations

import os
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
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
IMAGE_PROXY_TIMEOUT_SECONDS = int(os.environ.get("IMAGE_PROXY_TIMEOUT_SECONDS", "10"))
ALLOWED_IMAGE_HOSTS = tuple(
    x.strip().lower()
    for x in os.environ.get(
        "IMAGE_PROXY_ALLOWED_HOSTS",
        "i.ebayimg.com,img.leboncoin.fr,images.leboncoin.fr,ir.ebaystatic.com",
    ).split(",")
    if x.strip()
)

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


@app.get("/api/image-proxy")
def image_proxy(url: str = Query(min_length=8, max_length=1800)):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="invalid image url scheme")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise HTTPException(status_code=400, detail="invalid image host")
    allowed = any(host == h or host.endswith("." + h) for h in ALLOWED_IMAGE_HOSTS)
    if not allowed:
        raise HTTPException(status_code=400, detail="image host not allowed")

    headers = {
        "User-Agent": "PhoneRepairOffersBot/1.0 (+https://offers.actually-caring-about-billionaires.online)",
        "Accept": "image/*,*/*;q=0.8",
    }
    try:
        with httpx.Client(
            timeout=IMAGE_PROXY_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers=headers,
        ) as client:
            upstream = client.get(url)
            upstream.raise_for_status()
            content_type = (upstream.headers.get("Content-Type") or "").lower()
            if not content_type.startswith("image/"):
                raise RuntimeError("upstream is not an image")
            content = upstream.content[:3_500_000]
            return Response(
                content=content,
                media_type=content_type.split(";")[0],
                headers={"Cache-Control": "public, max-age=86400"},
            )
    except Exception:
        placeholder = BASE_DIR / "static" / "placeholder-offer.svg"
        return Response(
            content=placeholder.read_bytes(),
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=300"},
        )


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
