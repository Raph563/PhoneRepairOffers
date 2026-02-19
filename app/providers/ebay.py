from __future__ import annotations

import re
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

from app.services.offer_tools import (
    compute_offer_id,
    compute_rank_score,
    normalize_spaces,
    parse_price_to_eur,
)

BASE_URL = "https://www.ebay.fr"


def build_query(brand: str, model: str, part_type: str) -> str:
    if part_type == "replacement_screen":
        return f"ecran {brand} {model} remplacement"
    return f"{brand} {model} pour pieces sans ecran"


def extract_offer_id(url: str) -> str:
    m = re.search(r"/itm/(?:[^/]+/)?([0-9]{8,20})", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]item=([0-9]{8,20})", url)
    if m:
        return m.group(1)
    return url


def search_ebay(
    brand: str, model: str, part_type: str, max_price_eur: float | None, timeout_seconds: int = 18
) -> list[dict]:
    query = build_query(brand, model, part_type)
    max_price_param = ""
    if max_price_eur is not None and max_price_eur > 0:
        max_price_param = f"&_udhi={int(max_price_eur)}"

    url = (
        f"{BASE_URL}/sch/i.html?_nkw={quote_plus(query)}"
        f"&_sop=15&LH_BIN=1{max_price_param}&rt=nc"
    )

    headers = {
        "User-Agent": "PhoneRepairOffersBot/1.0 (+https://offers.actually-caring-about-billionaires.online)",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    }

    with httpx.Client(timeout=timeout_seconds, follow_redirects=True, headers=headers) as client:
        response = client.get(url)
        response.raise_for_status()
        html = response.text

    soup = BeautifulSoup(html, "lxml")
    offers: list[dict] = []

    for item in soup.select("li.s-item"):
        title_el = item.select_one(".s-item__title")
        link_el = item.select_one("a.s-item__link")
        if not title_el or not link_el:
            continue

        title = normalize_spaces(title_el.get_text(" ", strip=True))
        if not title or "Annonce" in title or title.lower() == "new listing":
            continue

        url_value = str(link_el.get("href") or "")
        if not url_value.startswith("http"):
            continue

        price_text = normalize_spaces(
            (item.select_one(".s-item__price") or title_el).get_text(" ", strip=True)
        )
        price_eur = parse_price_to_eur(price_text)
        if price_eur <= 0:
            continue

        shipping_text = normalize_spaces(
            (item.select_one(".s-item__shipping") or title_el).get_text(" ", strip=True)
        )
        shipping_eur = parse_price_to_eur(shipping_text)

        location = None
        location_el = item.select_one(".s-item__location")
        if location_el:
            location = normalize_spaces(location_el.get_text(" ", strip=True))

        condition_text = None
        cond_el = item.select_one(".SECONDARY_INFO")
        if cond_el:
            condition_text = normalize_spaces(cond_el.get_text(" ", strip=True))

        image_url = None
        image_el = item.select_one("img.s-item__image-img")
        if image_el:
            image_url = image_el.get("src") or image_el.get("data-src")

        source_offer_id = extract_offer_id(url_value)
        total = round(price_eur + shipping_eur, 2)

        offer_id = compute_offer_id("ebay", source_offer_id, url_value)
        offers.append(
            {
                "id": offer_id,
                "source": "ebay",
                "sourceOfferId": source_offer_id,
                "title": title,
                "url": url_value,
                "imageUrl": image_url,
                "priceEur": round(price_eur, 2),
                "shippingEur": round(shipping_eur, 2),
                "totalEur": total,
                "location": location,
                "conditionText": condition_text,
                "postedAt": None,
                "queryType": part_type,
                "rankScore": compute_rank_score(title, total),
            }
        )

    return offers[:120]
