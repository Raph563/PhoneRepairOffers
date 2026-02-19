from __future__ import annotations

import re
import threading
import time
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
EBAY_IMAGE_RE = re.compile(r"https://i\.ebayimg\.com/images/[^\s\)]+", re.IGNORECASE)
RECENT_IDS_CACHE_LOCK = threading.Lock()
RECENT_IDS_CACHE: dict[str, dict] = {}
RECENT_IDS_TTL_SECONDS = 900


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


def _recent_cache_key(query: str, max_price_eur: float | None) -> str:
    if max_price_eur is None:
        return f"{query}|none"
    return f"{query}|{int(max_price_eur)}"


def _extract_offer_ids_from_text(text: str, limit: int = 120) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"https://www\.ebay\.[^\s)\]]*/itm/[^\s)\]]+", text, re.IGNORECASE):
        offer_id = extract_offer_id(match.group(0))
        if not re.fullmatch(r"[0-9]{8,20}", offer_id):
            continue
        if offer_id == "123456" or offer_id in seen:
            continue
        seen.add(offer_id)
        ids.append(offer_id)
        if len(ids) >= limit:
            break
    return ids


def _fetch_recent_offer_ids(
    query: str, max_price_eur: float | None, timeout_seconds: int = 20
) -> set[str]:
    cache_key = _recent_cache_key(query, max_price_eur)
    now_ts = time.time()

    with RECENT_IDS_CACHE_LOCK:
        cached = RECENT_IDS_CACHE.get(cache_key)
        if cached and float(cached.get("expires_at") or 0) > now_ts:
            return set(cached.get("ids") or [])

    max_price_param = f"&_udhi={int(max_price_eur)}" if max_price_eur else ""
    recent_source_url = (
        f"{BASE_URL}/sch/i.html?_nkw={quote_plus(query)}&_sop=10&rt=nc{max_price_param}"
    )
    mirror_url = "https://r.jina.ai/http://" + recent_source_url.replace("https://", "")
    headers = {
        "User-Agent": "PhoneRepairOffersBot/1.0 (+https://offers.actually-caring-about-billionaires.online)",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    }

    try:
        with httpx.Client(
            timeout=timeout_seconds, follow_redirects=True, headers=headers
        ) as client:
            response = client.get(mirror_url)
            response.raise_for_status()
            ids = _extract_offer_ids_from_text(response.text, limit=140)
    except Exception:
        ids = []

    with RECENT_IDS_CACHE_LOCK:
        RECENT_IDS_CACHE[cache_key] = {
            "ids": ids,
            "expires_at": now_ts + RECENT_IDS_TTL_SECONDS,
        }
    return set(ids)


def _search_ebay_via_jina(
    brand: str, model: str, part_type: str, max_price_eur: float | None, timeout_seconds: int = 24
) -> list[dict]:
    query = build_query(brand, model, part_type)
    max_price_param = f"&_udhi={int(max_price_eur)}" if max_price_eur else ""
    source_url = f"{BASE_URL}/sch/i.html?_nkw={quote_plus(query)}&_sop=15&rt=nc{max_price_param}"
    mirror_url = "https://r.jina.ai/http://" + source_url.replace("https://", "")

    headers = {
        "User-Agent": "PhoneRepairOffersBot/1.0 (+https://offers.actually-caring-about-billionaires.online)",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    }
    with httpx.Client(timeout=timeout_seconds, follow_redirects=True, headers=headers) as client:
        response = client.get(mirror_url)
        response.raise_for_status()
        text = response.text

    offers: list[dict] = []
    # Mirror format usually exposes listing links plus nearby price text.
    pattern = re.compile(
        r"\[(?P<title>[^\]]+)\]\((?P<url>https://www\.ebay\.[^)]+/itm/[^)]+)\)(?P<tail>.{0,260})",
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(text):
        raw_title = normalize_spaces(match.group("title"))
        if (
            not raw_title
            or raw_title.lower().startswith("image ")
            or "shop on ebay" in raw_title.lower()
        ):
            continue

        title = raw_title.split("La page s'ouvre", 1)[0].strip()
        url_value = normalize_spaces(match.group("url"))
        if not title or not url_value:
            continue

        tail = normalize_spaces(match.group("tail"))
        price_eur = parse_price_to_eur(tail)
        if price_eur <= 0:
            continue

        image_url = None
        # The mirror often includes product image URLs near the listing link.
        left = max(0, match.start() - 700)
        right = min(len(text), match.end() + 80)
        near = text[left:right]
        image_candidates = EBAY_IMAGE_RE.findall(near)
        if image_candidates:
            image_url = normalize_spaces(image_candidates[-1].rstrip(".,;"))

        source_offer_id = extract_offer_id(url_value)
        total = round(price_eur, 2)
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
                "shippingEur": 0.0,
                "totalEur": total,
                "location": None,
                "conditionText": None,
                "postedAt": None,
                "queryType": part_type,
                "rankScore": compute_rank_score(title, total),
            }
        )

    # Dedup quickly by sourceOfferId while preserving order.
    seen: set[str] = set()
    filtered: list[dict] = []
    for row in offers:
        key = str(row.get("sourceOfferId"))
        if key in seen:
            continue
        seen.add(key)
        filtered.append(row)
    return filtered[:120]


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

    if not offers:
        offers = _search_ebay_via_jina(brand, model, part_type, max_price_eur, timeout_seconds=24)

    recent_ids = _fetch_recent_offer_ids(query, max_price_eur, timeout_seconds=20)
    for row in offers:
        row["isRecentlyAdded"] = str(row.get("sourceOfferId") or "") in recent_ids

    return offers[:120]
