from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse

import httpx
from bs4 import BeautifulSoup

from app.services.offer_tools import (
    compute_offer_id,
    compute_rank_score,
    normalize_spaces,
    parse_price_to_eur,
)

BASE_URL = "https://www.leboncoin.fr"
LEBONCOIN_IMAGE_RE = re.compile(
    r"https://(?:img|images)\.leboncoin\.fr/[^\s\)]+",
    re.IGNORECASE,
)


def build_query(
    brand: str,
    model: str,
    part_type: str,
    category: str = "mobile_phone_parts",
) -> str:
    if part_type == "replacement_screen":
        base = f"ecran {brand} {model} remplacement"
    else:
        base = f"{brand} {model} sans ecran pour pieces"
    if category == "mobile_phone_parts":
        return f"{base} telephones mobiles pieces"
    return base


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


def _search_leboncoin_via_jina(
    brand: str,
    model: str,
    part_type: str,
    max_price_eur: float | None,
    category: str = "mobile_phone_parts",
    timeout_seconds: int = 24,
) -> list[dict[str, Any]]:
    query = build_query(brand, model, part_type, category=category)
    max_price_param = ""
    if max_price_eur is not None and max_price_eur > 0:
        max_price_param = f"&price=min-{int(max_price_eur)}"
    source_url = f"{BASE_URL}/recherche?text={quote_plus(query)}{max_price_param}"
    mirror_url = "https://r.jina.ai/http://" + source_url.replace("https://", "")

    headers = {
        "User-Agent": "PhoneRepairOffersBot/1.0 (+https://offers.actually-caring-about-billionaires.online)",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    }
    with httpx.Client(timeout=timeout_seconds, follow_redirects=True, headers=headers) as client:
        response = client.get(mirror_url)
        response.raise_for_status()
        text = response.text

    offers: list[dict[str, Any]] = []
    pattern = re.compile(
        r"\[\]\((?P<url>https://www\.leboncoin\.fr/ad/[^\)]+)\)(?P<title>[^\n]+)\n(?P<tail>.{0,220})",
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(text):
        url = normalize_spaces(match.group("url"))
        title = normalize_spaces(match.group("title"))
        tail = normalize_spaces(match.group("tail"))
        if not url or not title:
            continue

        price_eur = parse_price_to_eur(tail)
        if price_eur <= 0:
            continue

        listing_id = url
        m = re.search(r"/([0-9]+)$", url)
        if m:
            listing_id = m.group(1)

        image_url = None
        left = max(0, match.start() - 900)
        right = min(len(text), match.end() + 900)
        near = text[left:right]
        image_candidates = LEBONCOIN_IMAGE_RE.findall(near)
        if image_candidates:
            image_url = normalize_spaces(image_candidates[-1].rstrip(".,;"))

        total_eur = round(price_eur, 2)
        offer_id = compute_offer_id("leboncoin", listing_id, url)
        offers.append(
            {
                "id": offer_id,
                "source": "leboncoin",
                "sourceOfferId": listing_id,
                "title": title,
                "url": url,
                "imageUrl": image_url,
                "priceEur": round(price_eur, 2),
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


def _search_leboncoin_via_duckduckgo(
    brand: str,
    model: str,
    part_type: str,
    max_price_eur: float | None,
    category: str = "mobile_phone_parts",
    timeout_seconds: int = 20,
) -> list[dict[str, Any]]:
    query = build_query(brand, model, part_type, category=category)
    ddg_url = f"https://duckduckgo.com/html/?q={quote_plus('site:leboncoin.fr ' + query)}"
    headers = {
        "User-Agent": "PhoneRepairOffersBot/1.0 (+https://offers.actually-caring-about-billionaires.online)",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    }
    with httpx.Client(timeout=timeout_seconds, follow_redirects=True, headers=headers) as client:
        response = client.get(ddg_url)
        response.raise_for_status()
        html = response.text

    soup = BeautifulSoup(html, "lxml")
    offers: list[dict[str, Any]] = []

    for card in soup.select(".result"):
        a = card.select_one("a.result__a")
        if not a:
            continue
        href = str(a.get("href") or "")
        if not href:
            continue

        if "duckduckgo.com/l/" in href:
            parsed = urlparse(href)
            target = parse_qs(parsed.query).get("uddg", [""])[0]
            if target:
                href = target
        href = normalize_spaces(href)
        if "leboncoin.fr" not in href:
            continue

        title = normalize_spaces(a.get_text(" ", strip=True))
        snippet = normalize_spaces(
            (card.select_one(".result__snippet").get_text(" ", strip=True))
            if card.select_one(".result__snippet")
            else ""
        )
        price = parse_price_to_eur(f"{title} {snippet}")
        if price <= 0:
            continue
        if max_price_eur is not None and max_price_eur > 0 and price > max_price_eur:
            continue

        listing_id = href
        m = re.search(r"/([0-9]+)(?:\\?|$)", href)
        if m:
            listing_id = m.group(1)

        image_url = None
        image_el = card.select_one("img[src]")
        if image_el:
            image_src = normalize_spaces(str(image_el.get("src") or ""))
            if image_src.startswith("//"):
                image_src = "https:" + image_src
            if image_src.startswith("http"):
                image_url = image_src

        total = round(price, 2)
        offer_id = compute_offer_id("leboncoin", listing_id, href)
        offers.append(
            {
                "id": offer_id,
                "source": "leboncoin",
                "sourceOfferId": listing_id,
                "title": title,
                "url": href,
                "imageUrl": image_url,
                "priceEur": round(price, 2),
                "shippingEur": 0.0,
                "totalEur": total,
                "location": None,
                "conditionText": None,
                "postedAt": None,
                "queryType": part_type,
                "rankScore": compute_rank_score(title, total),
            }
        )
    return offers[:80]


def search_leboncoin(
    brand: str,
    model: str,
    part_type: str,
    max_price_eur: float | None,
    category: str = "mobile_phone_parts",
    timeout_seconds: int = 18,
) -> list[dict[str, Any]]:
    query = build_query(brand, model, part_type, category=category)
    max_price_param = ""
    if max_price_eur is not None and max_price_eur > 0:
        max_price_param = f"&price=min-{int(max_price_eur)}"
    url = f"{BASE_URL}/recherche?text={quote_plus(query)}{max_price_param}"

    headers = {
        "User-Agent": "PhoneRepairOffersBot/1.0 (+https://offers.actually-caring-about-billionaires.online)",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    }

    try:
        with httpx.Client(
            timeout=timeout_seconds, follow_redirects=True, headers=headers
        ) as client:
            response = client.get(url)
            response.raise_for_status()
            html = response.text
    except httpx.HTTPError:
        mirror_offers = _search_leboncoin_via_jina(
            brand,
            model,
            part_type,
            max_price_eur,
            category=category,
            timeout_seconds=24,
        )
        if mirror_offers:
            return mirror_offers
        return _search_leboncoin_via_duckduckgo(
            brand,
            model,
            part_type,
            max_price_eur,
            category=category,
            timeout_seconds=20,
        )

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

    if offers:
        return offers[:120]

    mirror_offers = _search_leboncoin_via_jina(
        brand,
        model,
        part_type,
        max_price_eur,
        category=category,
        timeout_seconds=24,
    )
    if mirror_offers:
        return mirror_offers
    return _search_leboncoin_via_duckduckgo(
        brand,
        model,
        part_type,
        max_price_eur,
        category=category,
        timeout_seconds=20,
    )
