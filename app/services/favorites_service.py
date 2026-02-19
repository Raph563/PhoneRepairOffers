from __future__ import annotations

from app.db.database import Database
from app.db.models import Offer, ToggleFavoriteRequest


class FavoritesService:
    def __init__(self, db: Database):
        self.db = db

    def list_favorites(self) -> dict:
        return {"ok": True, "favorites": self.db.list_favorites()}

    def create_favorite(self, offer: Offer) -> dict:
        favorite_id = self.db.add_favorite(
            source=offer.source.value,
            source_offer_id=offer.sourceOfferId,
            offer_payload=offer.model_dump(mode="json"),
        )
        return {"ok": True, "favoriteId": favorite_id}

    def delete_favorite(self, favorite_id: int) -> dict:
        deleted = self.db.delete_favorite(favorite_id)
        return {"ok": deleted}

    def toggle_favorite(self, payload: ToggleFavoriteRequest) -> dict:
        existing = self.db.find_favorite_by_offer(payload.source.value, payload.sourceOfferId)
        if existing is not None:
            self.db.delete_favorite(existing)
            return {"ok": True, "isFavorite": False}

        if payload.offer is None:
            return {
                "ok": False,
                "isFavorite": False,
                "error": "offer payload required to create favorite",
            }

        favorite_id = self.db.add_favorite(
            source=payload.source.value,
            source_offer_id=payload.sourceOfferId,
            offer_payload=payload.offer.model_dump(mode="json"),
        )
        return {"ok": True, "isFavorite": True, "favoriteId": favorite_id}
