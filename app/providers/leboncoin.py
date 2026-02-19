from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

from app.services.offer_tools import (
    compute_offer_id,
    compute_rank_score,
    normalize_spaces,
    parse_price_to_eur,
)

BASE_URL = "https://www.leboncoin.fr"


def build_query(brand: str, model: str, part_type: str) -> str:
    if part_type == "replacement_screen":
        return f"ecran {brand} {model} remplacement"
    return f"{brand} {model} sans ecran pour pieces"


def _walk_for_ads(node: Any, out: list[dict[str, Any]]) -> None:
    if isinstance(node, dict):
        keys = set(node.keys())
        # Leboncoin payloads vary, keep generic patterns.
        if (
            ("subject" in keys or "title" in keys)
            and ("url" in keys)
            and ("price" in keys or "price_cents" in keys)
        ):
            out.append(node)
        for value in node.values():
            _walk_for_ads(value, out)
    elif isinstance(node, list):
        for value in node:
            _walk_for_ads(value, out)


def search_leboncoin(
    brand: str, model: str, part_type: str, max_price_eur: float | None, timeout_seconds: int = 18
) -> list[dict[str, Any]]:
    query = build_query(brand, model, part_type)
    max_price_param = ""
    if max_price_eur is not None and max_price_eur > 0:
        max_price_param = f"&price=min-{int(max_price_eur)}"
    url = f"{BASE_URL}/recherche?text={quote_plus(query)}{max_price_param}"

    headers = {
        "User-Agent": "PhoneRepairOffersBot/1.0 (+https://offers.actually-caring-about-billionaires.online)",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    }

    with httpx.Client(timeout=timeout_seconds, follow_redirects=True, headers=headers) as client:
        response = client.get(url)
        response.raise_for_status()
        html = response.text

    soup = BeautifulSoup(html, "lxml")
    candidates: list[dict[str, Any]] = []

    next_data = soup.select_one("#__NEXT_DATA__")
    if next_data and next_data.string:
        try:
            payload = json.loads(next_data.string)
            _walk_for_ads(payload, candidates)
        except Exception:
            pass

    offers: list[dict[str, Any]] = []

    for row in candidates:
        raw_title = row.get("subject") or row.get("title") or ""
        raw_url = row.get("url") or ""
        if not raw_title or not raw_url:
            continue

        listing_id = str(row.get("list_id") or row.get("ad_id") or row.get("id") or "")
        if not listing_id:
            m = re.search(r"/([0-9]+)\.htm", raw_url)
            listing_id = m.group(1) if m else raw_url

        price_value = 0.0
        if isinstance(row.get("price"), list) and row.get("price"):
            price_value = float(row["price"][0] or 0)
        elif isinstance(row.get("price"), (int, float)):
            price_value = float(row.get("price") or 0)
        elif row.get("price_cents"):
            price_value = float(row.get("price_cents") or 0) / 100

        if price_value <= 0:
            continue

        full_url = raw_url if str(raw_url).startswith("http") else f"{BASE_URL}{raw_url}"
        location = None
        if isinstance(row.get("location"), dict):
            location = row["location"].get("city")

        image_url = None
        images = row.get("images")
        if isinstance(images, dict):
            urls = images.get("urls")
            if isinstance(urls, dict):
                image_url = urls.get("small") or urls.get("thumb_url")

        total_eur = round(price_value, 2)
        offer_id = compute_offer_id("leboncoin", listing_id, full_url)
        title = normalize_spaces(str(raw_title))

        offers.append(
            {
                "id": offer_id,
                "source": "leboncoin",
                "sourceOfferId": listing_id,
                "title": title,
                "url": full_url,
                "imageUrl": image_url,
                "priceEur": round(price_value, 2),
                "shippingEur": 0.0,
                "totalEur": total_eur,
                "location": normalize_spaces(str(location)) if location else None,
                "conditionText": None,
                "postedAt": None,
                "queryType": part_type,
                "rankScore": compute_rank_score(title, total_eur),
            }
        )

    # Fallback parse if no __NEXT_DATA__ ads detected.
    if not offers:
        for anchor in soup.select("a[href*='/ad/'], a[href*='.htm']"):
            href = str(anchor.get("href") or "")
            title = normalize_spaces(anchor.get_text(" ", strip=True))
            if not href or not title:
                continue
            if len(title) < 6:
                continue
            card_text = normalize_spaces(
                anchor.parent.get_text(" ", strip=True) if anchor.parent else ""
            )
            price = parse_price_to_eur(card_text)
            if price <= 0:
                continue
            full_url = href if href.startswith("http") else f"{BASE_URL}{href}"
            listing_id = href
            m = re.search(r"/([0-9]+)\.htm", href)
            if m:
                listing_id = m.group(1)
            total_eur = round(price, 2)
            offer_id = compute_offer_id("leboncoin", listing_id, full_url)
            offers.append(
                {
                    "id": offer_id,
                    "source": "leboncoin",
                    "sourceOfferId": listing_id,
                    "title": title,
                    "url": full_url,
                    "imageUrl": None,
                    "priceEur": round(price, 2),
                    "shippingEur": 0.0,
                    "totalEur": total_eur,
                    "location": None,
                    "conditionText": None,
                    "postedAt": None,
                    "queryType": part_type,
                    "rankScore": compute_rank_score(title, total_eur),
                }
            )

    return offers[:120]
