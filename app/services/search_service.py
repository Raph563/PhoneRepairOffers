from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from app.db.database import Database
from app.db.models import SearchRequest
from app.providers.ebay import search_ebay
from app.providers.leboncoin import search_leboncoin
from app.services.offer_tools import dedupe_offers

ProviderFn = Callable[[str, str, str, float | None], list[dict[str, Any]]]


class SearchService:
    def __init__(self, db: Database, cache_ttl_seconds: int = 900):
        self.db = db
        self.cache_ttl_seconds = max(60, int(cache_ttl_seconds))
        self.providers: dict[str, ProviderFn] = {
            "leboncoin": search_leboncoin,
            "ebay": search_ebay,
        }

    @staticmethod
    def build_query_key(payload: dict[str, Any]) -> str:
        normalized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _provider_payload(self, req: SearchRequest) -> dict[str, Any]:
        return {
            "brand": req.brand.strip().lower(),
            "model": req.model.strip().lower(),
            "partType": req.partType.value,
            "maxPriceEur": req.maxPriceEur,
            "sources": sorted(source.value for source in req.sources),
        }

    def search(self, req: SearchRequest) -> dict[str, Any]:
        query_payload = self._provider_payload(req)
        query_key = self.build_query_key(query_payload)

        if not req.forceRefresh:
            cached = self.db.get_cached_search(query_key, ttl_seconds=self.cache_ttl_seconds)
            if cached is not None:
                return {
                    "ok": True,
                    "cached": True,
                    "queryKey": query_key,
                    "offers": cached.get("offers", []),
                    "providerErrors": cached.get("providerErrors", {}),
                }

        offers: list[dict[str, Any]] = []
        provider_errors: dict[str, str] = {}

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {}
            for source in req.sources:
                source_name = source.value
                fn = self.providers.get(source_name)
                if not fn:
                    provider_errors[source_name] = "provider_not_supported"
                    continue
                futures[
                    pool.submit(fn, req.brand, req.model, req.partType.value, req.maxPriceEur)
                ] = source_name

            for future in as_completed(futures):
                source_name = futures[future]
                try:
                    offers.extend(future.result())
                except Exception as err:
                    provider_errors[source_name] = str(err)

        offers = dedupe_offers(offers)
        offers.sort(key=lambda row: (float(row.get("totalEur", 0)), float(row.get("rankScore", 0))))

        payload = {
            "offers": offers,
            "providerErrors": provider_errors,
        }
        self.db.put_cached_search(query_key, payload)

        return {
            "ok": True,
            "cached": False,
            "queryKey": query_key,
            "offers": offers,
            "providerErrors": provider_errors,
        }
