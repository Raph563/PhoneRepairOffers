from __future__ import annotations

import html
import re
import threading
import time
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from app.services.offer_tools import (
    compute_offer_id,
    compute_rank_score,
    normalize_spaces,
    parse_price_to_eur,
)

BASE_URL = "https://fr.aliexpress.com"
ALI_ITEM_RE = re.compile(
    r"https://(?:(?:[a-z]{2,3}|www)\.)?aliexpress\.(?:com|us)/item/[0-9]{8,25}\.html(?:\?[^\s\)]*)?",
    re.IGNORECASE,
)
ALI_IMAGE_RE = re.compile(
    r"https://(?:ae\d+|img)\.alicdn\.com/[^\s\)]+",
    re.IGNORECASE,
)
PRICE_USD_RE = re.compile(r"\$([0-9]+(?:\.[0-9]{1,2})?)")
PRICE_EUR_RE = re.compile(r"([0-9]+(?:[.,][0-9]{1,2})?)\s*(?:€|EUR)", re.IGNORECASE)
FX_CACHE_LOCK = threading.Lock()
FX_CACHE: dict[str, dict] = {}
FX_CACHE_TTL_SECONDS = 43200
STATIC_FX_FALLBACK = {"USD": 0.92}


def build_query(
    brand: str, model: str, part_type: str, category: str = "mobile_phone_parts"
) -> str:
    if part_type == "replacement_screen":
        base = f"{brand} {model} replacement screen"
    else:
        base = f"{brand} {model} for parts phone"
    if category == "mobile_phone_parts":
        return base
    return base


def extract_offer_id(url: str) -> str:
    m = re.search(r"/item/([0-9]{8,25})\.html", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]itemId=([0-9]{8,25})", url)
    if m:
        return m.group(1)
    return url


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


def _normalize_item_url(raw_url: str) -> str:
    url_value = normalize_spaces(html.unescape(raw_url))
    if not url_value:
        return ""
    if url_value.startswith("//"):
        url_value = "https:" + url_value
    return url_value.rstrip(").,;")


def _get_fx_rate_to_eur(currency: str) -> float | None:
    cur = normalize_spaces(currency).upper()
    if not cur:
        return None
    if cur == "EUR":
        return 1.0

    now_ts = time.time()
    with FX_CACHE_LOCK:
        cached = FX_CACHE.get(cur)
        if cached and float(cached.get("expires_at") or 0) > now_ts:
            return float(cached.get("rate_to_eur"))

    rate = None
    try:
        url = f"https://open.er-api.com/v6/latest/{cur}"
        with httpx.Client(timeout=10, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            payload = response.json()
        rates = payload.get("rates") if isinstance(payload, dict) else None
        if isinstance(rates, dict):
            eur = rates.get("EUR")
            if isinstance(eur, (int, float)) and eur > 0:
                rate = float(eur)
    except Exception:
        rate = None

    if rate is None:
        rate = STATIC_FX_FALLBACK.get(cur)
    if rate is None:
        return None

    with FX_CACHE_LOCK:
        FX_CACHE[cur] = {
            "rate_to_eur": float(rate),
            "expires_at": now_ts + FX_CACHE_TTL_SECONDS,
        }
    return float(rate)


def _parse_pdp_npi_price_to_eur(url_value: str) -> tuple[float | None, str | None]:
    try:
        parsed = urlparse(url_value)
        pdp_npi_raw = parse_qs(parsed.query).get("pdp_npi", [""])[0]
    except Exception:
        pdp_npi_raw = ""
    if not pdp_npi_raw:
        return None, None

    decoded = unquote(pdp_npi_raw)
    parts = decoded.split("!")
    if len(parts) < 4:
        return None, None

    currency = normalize_spaces(parts[1]).upper()
    candidates: list[float] = []
    for idx in (3, 2):
        if idx >= len(parts):
            continue
        try:
            value = float(parts[idx])
        except Exception:
            continue
        if value > 0:
            candidates.append(value)
    if not candidates:
        return None, currency or None

    native_price = candidates[0]
    fx = _get_fx_rate_to_eur(currency or "EUR")
    if fx is None or fx <= 0:
        return None, currency or None
    return round(native_price * fx, 2), currency or None


def _parse_inline_price_to_eur(text: str) -> float | None:
    eur_match = PRICE_EUR_RE.search(text or "")
    if eur_match:
        value = parse_price_to_eur(eur_match.group(0))
        if value > 0:
            return round(value, 2)

    usd_candidates = [float(x) for x in PRICE_USD_RE.findall(text or "") if x]
    if usd_candidates:
        # The first $ amount is usually the current price in AliExpress snippets.
        fx = _get_fx_rate_to_eur("USD")
        if fx and fx > 0:
            return round(usd_candidates[0] * fx, 2)
    return None


def _extract_image_url(near_text: str) -> str | None:
    image_candidates = ALI_IMAGE_RE.findall(near_text or "")
    if not image_candidates:
        return None
    return normalize_spaces(image_candidates[-1].rstrip(".,;"))


def _build_offer(
    title: str,
    url_value: str,
    part_type: str,
    max_price_eur: float | None,
    price_eur: float | None,
    image_url: str | None = None,
    price_hint: str | None = None,
) -> dict | None:
    clean_title = normalize_spaces(title)
    clean_url = _normalize_item_url(url_value)
    clean_title = re.sub(r"https?://\S+", "", clean_title, flags=re.IGNORECASE)
    clean_title = re.sub(
        r"\b\S+\.(?:png|jpe?g|webp|avif)\)?",
        "",
        clean_title,
        flags=re.IGNORECASE,
    )
    clean_title = re.sub(r"\b[0-9]+(?:\.[0-9]+)?\s+sold\b.*$", "", clean_title, flags=re.IGNORECASE)
    clean_title = re.sub(r"\boff on\b.*$", "", clean_title, flags=re.IGNORECASE)
    clean_title = normalize_spaces(clean_title)
    if "aliexpress-media.com" in clean_title.lower() or "alicdn.com" in clean_title.lower():
        clean_title = ""
    if len(clean_title) < 8 and clean_url:
        clean_title = f"AliExpress {extract_offer_id(clean_url)}"

    if not clean_title or not clean_url:
        return None
    if price_eur is None or price_eur <= 0:
        return None
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
        "conditionText": price_hint,
        "postedAt": None,
        "queryType": part_type,
        "rankScore": compute_rank_score(clean_title, total),
    }


def _clean_markdown_title(raw_title: str) -> str:
    title = normalize_spaces(raw_title)
    title = re.sub(r"^#+\s*", "", title)
    title = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", title)
    title = normalize_spaces(title)
    # Remove trailing price chunk often appended in markdown summary.
    title = re.sub(r"\s+\$[0-9]+(?:\.[0-9]{1,2})?\s+\$[0-9]+(?:\.[0-9]{1,2})?.*$", "", title)
    return normalize_spaces(title)


def _extract_title_near_url(full_text: str, start_idx: int, end_idx: int) -> str:
    left = max(0, start_idx - 1400)
    right = min(len(full_text), end_idx + 80)
    window = full_text[left:right]

    # Typical r.jina block: "### <title> $xx.xx $yy.yy ..."
    h3_matches = list(
        re.finditer(r"###\s*(.+?)\s+\$[0-9]+(?:\.[0-9]{1,2})?", window, re.DOTALL)
    )
    if h3_matches:
        candidate = normalize_spaces(h3_matches[-1].group(1))
        candidate = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", candidate)
        candidate = normalize_spaces(candidate)
        if len(candidate) >= 8:
            return candidate

    # Try markdown title just before the URL.
    markdown_match = re.search(r"\[(?P<label>[^\n\]]{12,260})\]\([^)]*$", window)
    if markdown_match:
        candidate = _clean_markdown_title(markdown_match.group("label"))
        if len(candidate) >= 8:
            return candidate

    prefix = normalize_spaces(window[: max(0, start_idx - left)])
    prefix = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", prefix)
    prefix = normalize_spaces(prefix)
    # Keep only last chunk to avoid carrying previous cards.
    chunks = re.split(r"\s{2,}| \| ", prefix)
    candidate = normalize_spaces(chunks[-1] if chunks else prefix)
    candidate = re.sub(r"^#+\s*", "", candidate)
    candidate = re.sub(r"\$[0-9]+(?:\.[0-9]{1,2})?", "", candidate)
    candidate = normalize_spaces(candidate)
    if len(candidate) >= 8:
        return candidate
    return ""


def _search_aliexpress_via_jina(
    brand: str,
    model: str,
    part_type: str,
    max_price_eur: float | None,
    category: str = "mobile_phone_parts",
    timeout_seconds: int = 28,
) -> list[dict]:
    query = build_query(brand, model, part_type, category=category)
    source_url = f"{BASE_URL}/w/wholesale-{quote_plus(query)}.html?SortType=price_asc"
    mirror_url = "https://r.jina.ai/http://" + source_url.replace("https://", "")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        "Referer": "https://www.aliexpress.com/",
    }
    with httpx.Client(timeout=timeout_seconds, follow_redirects=True, headers=headers) as client:
        response = client.get(mirror_url)
        response.raise_for_status()
        text = response.text

    offers: list[dict] = []
    for match in ALI_ITEM_RE.finditer(text):
        url_value = _normalize_item_url(match.group(0))
        if not url_value:
            continue

        left = max(0, match.start() - 360)
        right = min(len(text), match.end() + 360)
        near = normalize_spaces(text[left:right])
        title = _extract_title_near_url(text, match.start(), match.end())

        price_eur, currency = _parse_pdp_npi_price_to_eur(url_value)
        if price_eur is None:
            price_eur = _parse_inline_price_to_eur(near)
        if price_eur is None:
            continue

        image_url = _extract_image_url(near)
        hint = None
        if currency and currency != "EUR":
            hint = f"Prix converti depuis {currency}"

        offer = _build_offer(
            title=title or f"AliExpress {extract_offer_id(url_value)}",
            url_value=url_value,
            part_type=part_type,
            max_price_eur=max_price_eur,
            price_eur=price_eur,
            image_url=image_url,
            price_hint=hint,
        )
        if offer:
            offers.append(offer)
    return _dedupe_by_offer_id(offers)[:120]


def _search_aliexpress_via_native_search_page(
    brand: str,
    model: str,
    part_type: str,
    max_price_eur: float | None,
    category: str = "mobile_phone_parts",
    timeout_seconds: int = 22,
) -> list[dict]:
    query = build_query(brand, model, part_type, category=category)
    url = f"{BASE_URL}/w/wholesale-{quote_plus(query)}.html?SortType=price_asc"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        "Referer": "https://www.aliexpress.com/",
    }
    with httpx.Client(timeout=timeout_seconds, follow_redirects=True, headers=headers) as client:
        response = client.get(url)
        response.raise_for_status()
        html = response.text

    soup = BeautifulSoup(html, "lxml")
    offers: list[dict] = []

    for anchor in soup.select("a[href*='/item/']"):
        href = _normalize_item_url(str(anchor.get("href") or ""))
        if not href or "aliexpress" not in href or "/item/" not in href:
            continue

        # Anchor text is often empty, fallback to parent text + product id.
        raw_title = normalize_spaces(anchor.get_text(" ", strip=True))
        parent_text = normalize_spaces(
            anchor.parent.get_text(" ", strip=True) if anchor.parent else ""
        )
        title = raw_title if len(raw_title) >= 8 else parent_text
        if len(title) < 8:
            title = f"AliExpress {extract_offer_id(href)}"

        price_eur, currency = _parse_pdp_npi_price_to_eur(href)
        if price_eur is None:
            price_eur = _parse_inline_price_to_eur(parent_text)
        if price_eur is None:
            continue

        image_url = None
        image_el = anchor.select_one("img[src]") or (
            anchor.parent.select_one("img[src]") if anchor.parent else None
        )
        if image_el:
            image_src = normalize_spaces(str(image_el.get("src") or image_el.get("data-src") or ""))
            if image_src.startswith("//"):
                image_src = "https:" + image_src
            if image_src.startswith("http"):
                image_url = image_src

        hint = None
        if currency and currency != "EUR":
            hint = f"Prix converti depuis {currency}"
        offer = _build_offer(
            title=title,
            url_value=href,
            part_type=part_type,
            max_price_eur=max_price_eur,
            price_eur=price_eur,
            image_url=image_url,
            price_hint=hint,
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
    source_url = f"https://lite.duckduckgo.com/lite/?q={quote_plus('site:fr.aliexpress.com/item ' + query)}"
    mirror_url = "https://r.jina.ai/http://" + source_url.replace("https://", "")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
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
        title = _clean_markdown_title(match.group("title"))
        ddg_redirect = normalize_spaces(match.group("ddg"))
        parsed = urlparse(ddg_redirect)
        target_encoded = parse_qs(parsed.query).get("uddg", [""])[0]
        if not target_encoded:
            continue
        target = _normalize_item_url(unquote(target_encoded))
        if "&rut=" in target:
            target = target.split("&rut=", 1)[0]
        if "aliexpress.com/item/" not in target and "aliexpress.us/item/" not in target:
            continue

        left = max(0, match.start() - 200)
        right = min(len(text), match.end() + 260)
        near = normalize_spaces(text[left:right])
        price_eur, currency = _parse_pdp_npi_price_to_eur(target)
        if price_eur is None:
            price_eur = _parse_inline_price_to_eur(near)
        if price_eur is None:
            continue

        hint = None
        if currency and currency != "EUR":
            hint = f"Prix converti depuis {currency}"
        offer = _build_offer(
            title=title or f"AliExpress {extract_offer_id(target)}",
            url_value=target,
            part_type=part_type,
            max_price_eur=max_price_eur,
            price_eur=price_eur,
            image_url=_extract_image_url(near),
            price_hint=hint,
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
    providers = [
        _search_aliexpress_via_native_search_page,
        _search_aliexpress_via_jina,
        _search_aliexpress_via_duckduckgo_lite,
    ]
    for provider in providers:
        try:
            offers = provider(
                brand,
                model,
                part_type,
                max_price_eur,
                category=category,
                timeout_seconds=max(20, timeout_seconds),
            )
        except Exception:
            offers = []
        if offers:
            return offers[:120]
    return []
