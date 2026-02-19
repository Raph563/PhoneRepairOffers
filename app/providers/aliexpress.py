from __future__ import annotations

import re
from urllib.parse import parse_qs, quote_plus, urlparse

import httpx
from bs4 import BeautifulSoup

from app.services.offer_tools import (
    compute_offer_id,
    compute_rank_score,
    normalize_spaces,
    parse_price_to_eur,
)

BASE_URL = "https://fr.aliexpress.com"
ALI_IMAGE_RE = re.compile(
    r"https://(?:ae\d+|img)\.alicdn\.com/[^\s\)]+",
    re.IGNORECASE,
)


def build_query(
    brand: str, model: str, part_type: str, category: str = "mobile_phone_parts"
) -> str:
    if part_type == "replacement_screen":
        base = f"ecran {brand} {model} remplacement"
    else:
        base = f"{brand} {model} telephone pour pieces sans ecran"
    if category == "mobile_phone_parts":
        return f"{base} telephone mobile pieces"
    return base


def extract_offer_id(url: str) -> str:
    m = re.search(r"/item/([0-9]{8,25})\.html", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]itemId=([0-9]{8,25})", url)
    if m:
        return m.group(1)
    return url


def _search_aliexpress_via_jina(
    brand: str,
    model: str,
    part_type: str,
    max_price_eur: float | None,
    category: str = "mobile_phone_parts",
    timeout_seconds: int = 24,
) -> list[dict]:
    query = build_query(brand, model, part_type, category=category)
    source_url = f"{BASE_URL}/w/wholesale-{quote_plus(query)}.html?SortType=price_asc"
    mirror_url = "https://r.jina.ai/http://" + source_url.replace("https://", "")

    headers = {
        "User-Agent": "PhoneRepairOffersBot/1.0 (+https://offers.actually-caring-about-billionaires.online)",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    }
    with httpx.Client(timeout=timeout_seconds, follow_redirects=True, headers=headers) as client:
        response = client.get(mirror_url)
        response.raise_for_status()
        text = response.text

    pattern = re.compile(
        r"\[(?P<title>[^\]]+)\]\((?P<url>https://fr\.aliexpress\.com/item/[^)]+)\)(?P<tail>.{0,280})",
        re.IGNORECASE | re.DOTALL,
    )
    offers: list[dict] = []
    for match in pattern.finditer(text):
        title = normalize_spaces(match.group("title"))
        url_value = normalize_spaces(match.group("url"))
        if not title or not url_value:
            continue

        tail = normalize_spaces(match.group("tail"))
        price_eur = parse_price_to_eur(f"{title} {tail}")
        if price_eur <= 0:
            continue
        if max_price_eur is not None and max_price_eur > 0 and price_eur > max_price_eur:
            continue

        image_url = None
        left = max(0, match.start() - 900)
        right = min(len(text), match.end() + 900)
        near = text[left:right]
        image_candidates = ALI_IMAGE_RE.findall(near)
        if image_candidates:
            image_url = normalize_spaces(image_candidates[-1].rstrip(".,;"))

        source_offer_id = extract_offer_id(url_value)
        total = round(price_eur, 2)
        offer_id = compute_offer_id("aliexpress", source_offer_id, url_value)
        offers.append(
            {
                "id": offer_id,
                "source": "aliexpress",
                "sourceOfferId": source_offer_id,
                "title": title,
                "url": url_value,
                "imageUrl": image_url,
                "priceEur": round(price_eur, 2),
                "shippingEur": 0.0,
                "totalEur": total,
                "location": None,
                "conditionText": None,
                "postedAt": None,
                "queryType": part_type,
                "rankScore": compute_rank_score(title, total),
            }
        )

    seen: set[str] = set()
    filtered: list[dict] = []
    for row in offers:
        key = str(row.get("sourceOfferId"))
        if key in seen:
            continue
        seen.add(key)
        filtered.append(row)
    return filtered[:120]


def _search_aliexpress_via_duckduckgo(
    brand: str,
    model: str,
    part_type: str,
    max_price_eur: float | None,
    category: str = "mobile_phone_parts",
    timeout_seconds: int = 20,
) -> list[dict]:
    query = build_query(brand, model, part_type, category=category)
    ddg_url = f"https://duckduckgo.com/html/?q={quote_plus('site:fr.aliexpress.com/item ' + query)}"
    headers = {
        "User-Agent": "PhoneRepairOffersBot/1.0 (+https://offers.actually-caring-about-billionaires.online)",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    }
    with httpx.Client(timeout=timeout_seconds, follow_redirects=True, headers=headers) as client:
        response = client.get(ddg_url)
        response.raise_for_status()
        html = response.text

    soup = BeautifulSoup(html, "lxml")
    offers: list[dict] = []

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
        if "aliexpress.com/item" not in href:
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

        image_url = None
        image_el = card.select_one("img[src]")
        if image_el:
            image_src = normalize_spaces(str(image_el.get("src") or ""))
            if image_src.startswith("//"):
                image_src = "https:" + image_src
            if image_src.startswith("http"):
                image_url = image_src

        source_offer_id = extract_offer_id(href)
        total = round(price, 2)
        offer_id = compute_offer_id("aliexpress", source_offer_id, href)
        offers.append(
            {
                "id": offer_id,
                "source": "aliexpress",
                "sourceOfferId": source_offer_id,
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

    return offers[:120]


def search_aliexpress(
    brand: str,
    model: str,
    part_type: str,
    max_price_eur: float | None,
    category: str = "mobile_phone_parts",
    timeout_seconds: int = 18,
) -> list[dict]:
    query = build_query(brand, model, part_type, category=category)
    url = f"{BASE_URL}/w/wholesale-{quote_plus(query)}.html?SortType=price_asc"

    headers = {
        "User-Agent": "PhoneRepairOffersBot/1.0 (+https://offers.actually-caring-about-billionaires.online)",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    }

    try:
        with httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=True,
            headers=headers,
        ) as client:
            response = client.get(url)
            response.raise_for_status()
            html = response.text
    except httpx.HTTPError:
        mirror_offers = _search_aliexpress_via_jina(
            brand,
            model,
            part_type,
            max_price_eur,
            category=category,
            timeout_seconds=24,
        )
        if mirror_offers:
            return mirror_offers
        return _search_aliexpress_via_duckduckgo(
            brand,
            model,
            part_type,
            max_price_eur,
            category=category,
            timeout_seconds=20,
        )

    soup = BeautifulSoup(html, "lxml")
    offers: list[dict] = []
    for anchor in soup.select("a[href*='/item/']"):
        href = normalize_spaces(str(anchor.get("href") or ""))
        if not href:
            continue
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = BASE_URL + href
        if "aliexpress.com/item/" not in href:
            continue

        title = normalize_spaces(anchor.get_text(" ", strip=True))
        if len(title) < 5:
            continue

        card_text = normalize_spaces(
            anchor.parent.get_text(" ", strip=True) if anchor.parent else ""
        )
        price = parse_price_to_eur(f"{title} {card_text}")
        if price <= 0:
            continue
        if max_price_eur is not None and max_price_eur > 0 and price > max_price_eur:
            continue

        image_url = None
        image_el = anchor.select_one("img[src]") or (
            anchor.parent.select_one("img[src]") if anchor.parent else None
        )
        if image_el:
            image_src = normalize_spaces(
                str(image_el.get("src") or image_el.get("data-src") or "")
            )
            if image_src.startswith("//"):
                image_src = "https:" + image_src
            if image_src.startswith("http"):
                image_url = image_src

        source_offer_id = extract_offer_id(href)
        total = round(price, 2)
        offer_id = compute_offer_id("aliexpress", source_offer_id, href)
        offers.append(
            {
                "id": offer_id,
                "source": "aliexpress",
                "sourceOfferId": source_offer_id,
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

    if offers:
        return offers[:120]

    mirror_offers = _search_aliexpress_via_jina(
        brand,
        model,
        part_type,
        max_price_eur,
        category=category,
        timeout_seconds=24,
    )
    if mirror_offers:
        return mirror_offers
    return _search_aliexpress_via_duckduckgo(
        brand,
        model,
        part_type,
        max_price_eur,
        category=category,
        timeout_seconds=20,
    )
