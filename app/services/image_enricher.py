from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

IMAGE_CACHE_LOCK = threading.Lock()
IMAGE_CACHE: dict[str, dict] = {}
IMAGE_CACHE_TTL_SECONDS = int(os.environ.get("IMAGE_CACHE_TTL_SECONDS", "21600"))


class ImageEnricher:
    def __init__(self, enabled: bool = True, max_per_search: int = 40, timeout_seconds: int = 8):
        self.enabled = enabled
        self.max_per_search = max(0, int(max_per_search))
        self.timeout_seconds = max(3, int(timeout_seconds))

    @staticmethod
    def _normalize_image_url(page_url: str, image_url: str | None) -> str | None:
        if not image_url:
            return None
        url = str(image_url).strip()
        if not url:
            return None
        if url.startswith("//"):
            return f"https:{url}"
        if url.startswith("/"):
            return urljoin(page_url, url)
        return url

    def _cache_get(self, url: str) -> Optional[str]:
        now_ts = time.time()
        with IMAGE_CACHE_LOCK:
            row = IMAGE_CACHE.get(url)
            if not row:
                return None
            expires_at = float(row.get("expires_at") or 0)
            if expires_at <= now_ts:
                IMAGE_CACHE.pop(url, None)
                return None
            return row.get("image_url")

    def _cache_set(self, url: str, image_url: str | None) -> None:
        now_ts = time.time()
        with IMAGE_CACHE_LOCK:
            IMAGE_CACHE[url] = {
                "image_url": image_url,
                "expires_at": now_ts + max(60, IMAGE_CACHE_TTL_SECONDS),
            }

    def _extract_image(self, page_url: str, html: str) -> str | None:
        soup = BeautifulSoup(html, "lxml")

        checks = [
            ("meta[property='og:image']", "content"),
            ("meta[property='og:image:url']", "content"),
            ("meta[name='twitter:image']", "content"),
            ("meta[name='twitter:image:src']", "content"),
        ]
        for selector, attr in checks:
            node = soup.select_one(selector)
            if not node:
                continue
            value = node.get(attr)
            normalized = self._normalize_image_url(page_url, value)
            if normalized:
                return normalized

        img = soup.select_one("img[src]")
        if img:
            normalized = self._normalize_image_url(page_url, img.get("src"))
            if normalized:
                return normalized
        return None

    def _fetch_image_for_offer(self, offer: dict) -> tuple[str, str | None]:
        url = str(offer.get("url") or "").strip()
        if not url:
            return "", None

        host = (urlparse(url).hostname or "").lower()
        if host in {"example.com", "localhost", "127.0.0.1"}:
            return url, None

        cached = self._cache_get(url)
        if cached is not None:
            return url, cached

        headers = {
            "User-Agent": "PhoneRepairOffersBot/1.0 (+https://offers.actually-caring-about-billionaires.online)",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        }
        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                follow_redirects=True,
                headers=headers,
            ) as client:
                response = client.get(url)
                response.raise_for_status()
                image_url = self._extract_image(url, response.text)
        except Exception:
            image_url = None

        self._cache_set(url, image_url)
        return url, image_url

    def enrich(self, offers: list[dict]) -> None:
        if not self.enabled or not offers or self.max_per_search <= 0:
            return

        targets = [row for row in offers if not row.get("imageUrl") and row.get("url")]
        if not targets:
            return
        targets = targets[: self.max_per_search]

        with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
            futures = {pool.submit(self._fetch_image_for_offer, row): row for row in targets}
            for future in as_completed(futures):
                row = futures[future]
                try:
                    _url, image = future.result()
                except Exception:
                    image = None
                if image:
                    row["imageUrl"] = image
