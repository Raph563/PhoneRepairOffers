from __future__ import annotations

import re
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from app.services.offer_tools import (
    compute_offer_id,
    compute_rank_score,
    normalize_spaces,
)

BASE_URL = "https://fr.aliexpress.com"
ALI_IMAGE_RE = re.compile(
    r"https://(?:ae\d+|img)\.alicdn\.com/[^\s\)]+",
    re.IGNORECASE,
)
ALI_ITEM_RE = re.compile(
    r"https://(?:[a-z]{2}\.)?aliexpress\.com/item/[0-9]{8,25}\.html(?:\?[^\s\)]*)?",
    re.IGNORECASE,
)
PRICE_WITH_CURRENCY_RE = re.compile(
    r"([0-9]+(?:[.,][0-9]{1,2})?)\s*(?:€|eur|us\$)",
    re.IGNORECASE,
)
UNKNOWN_PRICE_FALLBACK = 999.0


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


def _parse_explicit_price(text: str) -> float:
    match = PRICE_WITH_CURRENCY_RE.search(text or "")
    if not match:
        return 0.0
    raw = match.group(1).replace(",", ".")
    try:
        return round(float(raw), 2)
    except ValueError:
        return 0.0


def _resolve_unknown_price(max_price_eur: float | None) -> float | None:
    if max_price_eur is not None and max_price_eur > 0:
        return None
    return UNKNOWN_PRICE_FALLBACK


def _build_offer(
    title: str,
    url_value: str,
    part_type: str,
    max_price_eur: float | None,
    text_for_price: str,
    image_url: str | None = None,
) -> dict | None:
    clean_title = normalize_spaces(title)
    clean_url = normalize_spaces(url_value)
    if not clean_title or not clean_url:
        return None

    price_eur = _parse_explicit_price(text_for_price)
    if price_eur <= 0:
        fallback_price = _resolve_unknown_price(max_price_eur)
        if fallback_price is None:
            return None
        price_eur = fallback_price

    if max_price_eur is not None and max_price_eur > 0 and price_eur > max_price_eur:
        return None

    source_offer_id = extract_offer_id(clean_url)
    total = round(price_eur, 2)
    offer_id = compute_offer_id("aliexpress", source_offer_id, clean_url)
    return {
        "id": offer_id,
        "source": "aliexpress",
        "sourceOfferId": source_offer_id,
        "title": clean_title,
        "url": clean_url,
        "imageUrl": image_url,
        "priceEur": round(price_eur, 2),
        "shippingEur": 0.0,
        "totalEur": total,
        "location": None,
        "conditionText": "Prix non detecte automatiquement" if price_eur == UNKNOWN_PRICE_FALLBACK else None,
        "postedAt": None,
        "queryType": part_type,
        "rankScore": compute_rank_score(clean_title, total),
    }


def _dedupe_by_offer_id(offers: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result: list[dict] = []
    for row in offers:
        key = str(row.get("sourceOfferId") or "")
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


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

    offers: list[dict] = []
    for match in ALI_ITEM_RE.finditer(text):
        url_value = normalize_spaces(match.group(0))
        left = max(0, match.start() - 400)
        right = min(len(text), match.end() + 600)
        near = normalize_spaces(text[left:right])
        title_match = re.search(r"\[([^\]]+)\]\(" + re.escape(url_value) + r"\)", near)
        title = normalize_spaces(title_match.group(1)) if title_match else "Annonce AliExpress"

        image_url = None
        image_candidates = ALI_IMAGE_RE.findall(near)
        if image_candidates:
            image_url = normalize_spaces(image_candidates[-1].rstrip(".,;"))

        offer = _build_offer(
            title=title,
            url_value=url_value,
            part_type=part_type,
            max_price_eur=max_price_eur,
            text_for_price=near,
            image_url=image_url,
        )
        if offer:
            offers.append(offer)

    return _dedupe_by_offer_id(offers)[:120]


def _search_aliexpress_via_duckduckgo_html(
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
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        "Referer": "https://duckduckgo.com/",
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
        if "duckduckgo.com/l/" in href:
            parsed = urlparse(href)
            target = parse_qs(parsed.query).get("uddg", [""])[0]
            if target:
                href = target
        href = normalize_spaces(href)
        if "aliexpress.com/item/" not in href:
            continue

        title = normalize_spaces(a.get_text(" ", strip=True))
        snippet_el = card.select_one(".result__snippet")
        snippet = normalize_spaces(snippet_el.get_text(" ", strip=True) if snippet_el else "")
        image_url = None
        image_el = card.select_one("img[src]")
        if image_el:
            src = normalize_spaces(str(image_el.get("src") or ""))
            if src.startswith("//"):
                src = "https:" + src
            if src.startswith("http"):
                image_url = src

        offer = _build_offer(
            title=title,
            url_value=href,
            part_type=part_type,
            max_price_eur=max_price_eur,
            text_for_price=f"{title} {snippet}",
            image_url=image_url,
        )
        if offer:
            offers.append(offer)

    return _dedupe_by_offer_id(offers)[:120]


def _search_aliexpress_via_duckduckgo_lite(
    brand: str,
    model: str,
    part_type: str,
    max_price_eur: float | None,
    category: str = "mobile_phone_parts",
    timeout_seconds: int = 20,
) -> list[dict]:
    query = build_query(brand, model, part_type, category=category)
    ddg_url = f"https://lite.duckduckgo.com/lite/?q={quote_plus('site:fr.aliexpress.com/item ' + query)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        "Referer": "https://duckduckgo.com/",
    }
    with httpx.Client(timeout=timeout_seconds, follow_redirects=True, headers=headers) as client:
        response = client.get(ddg_url)
        response.raise_for_status()
        html = response.text

    soup = BeautifulSoup(html, "lxml")
    offers: list[dict] = []

    for a in soup.select("a[href]"):
        href = normalize_spaces(str(a.get("href") or ""))
        if not href:
            continue
        if "duckduckgo.com/l/" in href:
            parsed = urlparse(href)
            target = parse_qs(parsed.query).get("uddg", [""])[0]
            if target:
                href = target
        if "aliexpress.com/item/" not in href:
            continue

        title = normalize_spaces(a.get_text(" ", strip=True))
        if len(title) < 5:
            continue

        row = a.find_parent("tr")
        row_text = normalize_spaces(row.get_text(" ", strip=True) if row else title)
        offer = _build_offer(
            title=title,
            url_value=href,
            part_type=part_type,
            max_price_eur=max_price_eur,
            text_for_price=row_text,
            image_url=None,
        )
        if offer:
            offers.append(offer)

    return _dedupe_by_offer_id(offers)[:120]


def _search_aliexpress_via_jina_duckduckgo_lite(
    brand: str,
    model: str,
    part_type: str,
    max_price_eur: float | None,
    category: str = "mobile_phone_parts",
    timeout_seconds: int = 24,
) -> list[dict]:
    query = build_query(brand, model, part_type, category=category)
    source_url = f"https://lite.duckduckgo.com/lite/?q={quote_plus('site:fr.aliexpress.com/item ' + query)}"
    mirror_url = "https://r.jina.ai/http://" + source_url.replace("https://", "")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        "Referer": "https://duckduckgo.com/",
    }
    with httpx.Client(timeout=timeout_seconds, follow_redirects=True, headers=headers) as client:
        response = client.get(mirror_url)
        response.raise_for_status()
        text = response.text

    offers: list[dict] = []
    pattern = re.compile(
        r"\[[^\]]*?(?P<title>[^\]]+)\]\((?P<ddg>https://duckduckgo\.com/l/\?[^)]+)\)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        title = normalize_spaces(match.group("title"))
        ddg_redirect = normalize_spaces(match.group("ddg"))
        parsed = urlparse(ddg_redirect)
        target_encoded = parse_qs(parsed.query).get("uddg", [""])[0]
        if not target_encoded:
            continue
        target = normalize_spaces(unquote(target_encoded))
        if "&rut=" in target:
            target = target.split("&rut=", 1)[0]
        if "aliexpress.com/item/" not in target:
            continue

        line_left = max(0, match.start() - 120)
        line_right = min(len(text), match.end() + 220)
        near = normalize_spaces(text[line_left:line_right])

        offer = _build_offer(
            title=title,
            url_value=target,
            part_type=part_type,
            max_price_eur=max_price_eur,
            text_for_price=near,
            image_url=None,
        )
        if offer:
            offers.append(offer)

    return _dedupe_by_offer_id(offers)[:120]


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
        html = ""

    offers: list[dict] = []
    if html:
        soup = BeautifulSoup(html, "lxml")
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

            image_url = None
            image_el = anchor.select_one("img[src]") or (
                anchor.parent.select_one("img[src]") if anchor.parent else None
            )
            if image_el:
                src = normalize_spaces(str(image_el.get("src") or image_el.get("data-src") or ""))
                if src.startswith("//"):
                    src = "https:" + src
                if src.startswith("http"):
                    image_url = src

            card_text = normalize_spaces(
                anchor.parent.get_text(" ", strip=True) if anchor.parent else title
            )
            offer = _build_offer(
                title=title,
                url_value=href,
                part_type=part_type,
                max_price_eur=max_price_eur,
                text_for_price=card_text,
                image_url=image_url,
            )
            if offer:
                offers.append(offer)

    if offers:
        return _dedupe_by_offer_id(offers)[:120]

    try:
        mirror_offers = _search_aliexpress_via_jina(
            brand,
            model,
            part_type,
            max_price_eur,
            category=category,
            timeout_seconds=24,
        )
    except Exception:
        mirror_offers = []
    if mirror_offers:
        return mirror_offers

    try:
        jina_ddg_lite_offers = _search_aliexpress_via_jina_duckduckgo_lite(
            brand,
            model,
            part_type,
            max_price_eur,
            category=category,
            timeout_seconds=24,
        )
    except Exception:
        jina_ddg_lite_offers = []
    if jina_ddg_lite_offers:
        return jina_ddg_lite_offers

    try:
        ddg_offers = _search_aliexpress_via_duckduckgo_html(
            brand,
            model,
            part_type,
            max_price_eur,
            category=category,
            timeout_seconds=20,
        )
    except Exception:
        ddg_offers = []
    if ddg_offers:
        return ddg_offers

    try:
        return _search_aliexpress_via_duckduckgo_lite(
            brand,
            model,
            part_type,
            max_price_eur,
            category=category,
            timeout_seconds=20,
        )
    except Exception:
        return []
