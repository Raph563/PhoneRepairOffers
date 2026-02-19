from __future__ import annotations

import importlib

from fastapi.testclient import TestClient


def _sample_offer(source: str, offer_id: str, total: float) -> dict:
    return {
        "id": f"{source}-{offer_id}",
        "source": source,
        "sourceOfferId": offer_id,
        "title": f"Offer {offer_id}",
        "url": f"https://example.com/{source}/{offer_id}",
        "imageUrl": None,
        "priceEur": total,
        "shippingEur": 0.0,
        "totalEur": total,
        "location": "Paris",
        "conditionText": None,
        "postedAt": None,
        "queryType": "replacement_screen",
        "rankScore": total,
    }


def test_search_cache_and_favorites(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "offers.db"))
    monkeypatch.setenv("CACHE_TTL_SECONDS", "900")
    monkeypatch.setenv("APP_VERSION", "test")

    import app.main as main_mod

    importlib.reload(main_mod)

    calls = {"ebay": 0, "leboncoin": 0}

    def ebay_provider(*_args, **_kwargs):
        calls["ebay"] += 1
        return [_sample_offer("ebay", "111", 55.0)]

    def lbc_provider(*_args, **_kwargs):
        calls["leboncoin"] += 1
        return [_sample_offer("leboncoin", "222", 49.0)]

    main_mod.search_service.providers = {
        "ebay": ebay_provider,
        "leboncoin": lbc_provider,
    }

    client = TestClient(main_mod.app)

    payload = {
        "brand": "Samsung",
        "model": "S21",
        "partType": "replacement_screen",
        "sources": ["ebay", "leboncoin"],
    }

    r1 = client.post("/api/search", json=payload)
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["ok"] is True
    assert d1["cached"] is False
    assert len(d1["offers"]) == 2
    assert calls["ebay"] == 1
    assert calls["leboncoin"] == 1

    r2 = client.post("/api/search", json=payload)
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["cached"] is True
    assert calls["ebay"] == 1
    assert calls["leboncoin"] == 1

    offer = d1["offers"][0]
    toggled = client.post(
        "/api/favorites/toggle",
        json={
            "source": offer["source"],
            "sourceOfferId": offer["sourceOfferId"],
            "offer": offer,
        },
    )
    assert toggled.status_code == 200
    assert toggled.json()["isFavorite"] is True

    favs = client.get("/api/favorites")
    assert favs.status_code == 200
    assert len(favs.json()["favorites"]) == 1

    toggled2 = client.post(
        "/api/favorites/toggle",
        json={
            "source": offer["source"],
            "sourceOfferId": offer["sourceOfferId"],
            "offer": offer,
        },
    )
    assert toggled2.status_code == 200
    assert toggled2.json()["isFavorite"] is False

    favs2 = client.get("/api/favorites")
    assert len(favs2.json()["favorites"]) == 0
