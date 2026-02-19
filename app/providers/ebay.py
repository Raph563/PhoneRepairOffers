from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote_plus, urlparse, urlunparse

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
VARIATION_CACHE_LOCK = threading.Lock()
VARIATION_CACHE: dict[str, dict] = {}
VARIATION_CACHE_TTL_SECONDS = int(os.environ.get("EBAY_VARIATION_CACHE_TTL_SECONDS", "21600"))
FR_VARIANT_PRICE_CACHE_LOCK = threading.Lock()
FR_VARIANT_PRICE_CACHE: dict[str, dict] = {}
FR_VARIANT_PRICE_CACHE_TTL_SECONDS = int(
    os.environ.get("EBAY_FR_VARIANT_PRICE_CACHE_TTL_SECONDS", "21600")
)
VARIANT_ENRICH_MAX_OFFERS = int(os.environ.get("EBAY_VARIANT_ENRICH_MAX_OFFERS", "10"))


def build_query(
    brand: str, model: str, part_type: str, category: str = "mobile_phone_parts"
) -> str:
    if part_type == "replacement_screen":
        base = f"ecran {brand} {model} remplacement"
    else:
        base = f"{brand} {model} pour pieces sans ecran"
    if category == "mobile_phone_parts":
        return f"{base} telephone mobile pieces"
    return base


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


def _fold_text(value: str) -> str:
    clean = unicodedata.normalize("NFKD", value or "")
    clean = "".join(ch for ch in clean if not unicodedata.combining(ch))
    return normalize_spaces(clean).lower()


def _canonical_item_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))


def _extract_json_object_from(text: str, start_idx: int) -> str | None:
    if start_idx < 0:
        return None
    idx = start_idx
    while idx < len(text) and text[idx] != "{":
        idx += 1
    if idx >= len(text):
        return None

    depth = 0
    in_str = False
    escaped = False
    for i in range(idx, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[idx : i + 1]
    return None


def _extract_numeric(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        parsed = parse_price_to_eur(value)
        return parsed if parsed > 0 else None
    if isinstance(value, dict):
        for key in ("value", "amount", "min", "max"):
            if key in value:
                nested = _extract_numeric(value.get(key))
                if nested is not None and nested > 0:
                    return nested
    if isinstance(value, list):
        for row in value:
            nested = _extract_numeric(row)
            if nested is not None and nested > 0:
                return nested
    return None


def _extract_variation_price_info(variation_row: dict) -> tuple[float | None, str | None]:
    candidates = (
        variation_row.get("binModel", {}).get("price"),
        variation_row.get("binModel", {}).get("currentPrice"),
        variation_row.get("binModel", {}).get("displayPrice"),
        variation_row.get("price"),
    )
    for candidate in candidates:
        currency = None
        if isinstance(candidate, dict):
            value = candidate.get("value")
            if isinstance(value, dict):
                currency = str(value.get("currency") or "").upper() or None
            elif isinstance(candidate.get("currency"), str):
                currency = str(candidate.get("currency") or "").upper() or None
        numeric = _extract_numeric(candidate)
        if numeric is not None and numeric > 0:
            return round(float(numeric), 2), currency
    return None, None


def _extract_variation_shipping(variation_row: dict) -> float:
    candidates = (
        variation_row.get("shippingCost"),
        variation_row.get("shipping"),
        variation_row.get("deliveryCost"),
        variation_row.get("delivery"),
    )
    for candidate in candidates:
        numeric = _extract_numeric(candidate)
        if numeric is not None and numeric >= 0:
            return round(float(numeric), 2)
    return 0.0


def _parse_fr_eur_value(raw_value: str) -> float | None:
    text = normalize_spaces(raw_value).replace(" ", "").replace(".", "").replace(",", ".")
    try:
        value = float(text)
    except Exception:
        return None
    if value <= 0:
        return None
    return round(value, 2)


def _extract_fr_variant_price_from_text(page_text: str) -> float | None:
    pattern = re.compile(r"([0-9]{1,3}(?:[ .][0-9]{3})*,[0-9]{2})\s*EUR", re.IGNORECASE)
    state_idx = page_text.find("État:")
    if state_idx < 0:
        state_idx = page_text.find("Etat:")
    if state_idx < 0:
        state_idx = page_text.find("Condition:")
    if state_idx > 0:
        segment = page_text[max(0, state_idx - 1400) : state_idx + 200]
        matches = list(pattern.finditer(segment))
        if matches:
            value = _parse_fr_eur_value(matches[-1].group(1))
            if value is not None:
                return value

    model_idx = page_text.find("Modèle compatible")
    if model_idx < 0:
        model_idx = page_text.find("Modele compatible")
    if model_idx > 0:
        segment = page_text[max(0, model_idx - 1400) : model_idx + 200]
        matches = list(pattern.finditer(segment))
        if matches:
            value = _parse_fr_eur_value(matches[-1].group(1))
            if value is not None:
                return value
    return None


def _fetch_fr_variant_price(item_id: str, variation_id: str, timeout_seconds: int = 18) -> float | None:
    if not item_id or not variation_id:
        return None
    cache_key = f"{item_id}:{variation_id}"
    now_ts = time.time()
    with FR_VARIANT_PRICE_CACHE_LOCK:
        cached = FR_VARIANT_PRICE_CACHE.get(cache_key)
        if cached and float(cached.get("expires_at") or 0) > now_ts:
            return cached.get("price_eur")

    price_value = None
    url = f"https://r.jina.ai/http://www.ebay.fr/itm/{item_id}?var={variation_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    }
    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=True, headers=headers) as client:
            response = client.get(url)
            response.raise_for_status()
            price_value = _extract_fr_variant_price_from_text(response.text)
    except Exception:
        price_value = None

    with FR_VARIANT_PRICE_CACHE_LOCK:
        FR_VARIANT_PRICE_CACHE[cache_key] = {
            "price_eur": price_value,
            "expires_at": now_ts + max(300, FR_VARIANT_PRICE_CACHE_TTL_SECONDS),
        }
    return price_value


def _extract_msku_data(html: str) -> dict | None:
    marker = '"MSKU":{"_type":"VariationViewModel"'
    idx = html.find(marker)
    if idx < 0:
        return None
    value_start = html.find("{", idx + len('"MSKU":'))
    if value_start < 0:
        return None
    payload = _extract_json_object_from(html, value_start)
    if not payload:
        return None
    try:
        data = json.loads(payload)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _build_variation_options(msku_data: dict) -> list[dict]:
    menu_item_map = msku_data.get("menuItemMap") or {}
    variations_map = msku_data.get("variationsMap") or {}
    if not isinstance(menu_item_map, dict) or not isinstance(variations_map, dict):
        return []

    options: list[dict] = []
    for row in menu_item_map.values():
        if not isinstance(row, dict):
            continue
        name = normalize_spaces(str(row.get("displayName") or row.get("valueName") or ""))
        if not name:
            continue
        variation_ids = [str(x) for x in (row.get("matchingVariationIds") or []) if x is not None]
        if not variation_ids:
            continue

        best_price = None
        best_shipping = 0.0
        best_currency = None
        for variation_id in variation_ids:
            variation = variations_map.get(variation_id)
            if not isinstance(variation, dict):
                continue
            price, currency = _extract_variation_price_info(variation)
            if price is None:
                continue
            shipping = _extract_variation_shipping(variation)
            if best_price is None or price < best_price:
                best_price = price
                best_shipping = shipping
                best_currency = currency

        if best_price is None:
            continue
        options.append(
            {
                "name": name,
                "variationIds": variation_ids,
                "priceEur": round(best_price, 2),
                "shippingEur": round(best_shipping, 2),
                "currency": best_currency,
            }
        )
    return options


def _get_item_variation_options(item_url: str, timeout_seconds: int = 16) -> list[dict]:
    cache_key = _canonical_item_url(item_url)
    now_ts = time.time()

    with VARIATION_CACHE_LOCK:
        cached = VARIATION_CACHE.get(cache_key)
        if cached and float(cached.get("expires_at") or 0) > now_ts:
            return list(cached.get("options") or [])

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    }
    options: list[dict] = []
    fetch_urls = [cache_key]
    if "www.ebay.fr" in cache_key:
        fetch_urls.append(cache_key.replace("www.ebay.fr", "www.ebay.com"))
    elif "www.ebay.com" in cache_key:
        fetch_urls.append(cache_key.replace("www.ebay.com", "www.ebay.fr"))

    for fetch_url in fetch_urls:
        try:
            with httpx.Client(
                timeout=timeout_seconds,
                follow_redirects=True,
                headers=headers,
                http2=False,
            ) as client:
                response = client.get(fetch_url)
                response.raise_for_status()
                html = response.text
            msku_data = _extract_msku_data(html)
            if not msku_data:
                continue
            options = _build_variation_options(msku_data)
            if options:
                break
        except Exception:
            continue

    ttl = VARIATION_CACHE_TTL_SECONDS if options else 600
    with VARIATION_CACHE_LOCK:
        VARIATION_CACHE[cache_key] = {
            "options": options,
            "expires_at": now_ts + max(60, ttl),
        }
    return options


def _score_model_option(option_name: str, model: str) -> float:
    option_fold = _fold_text(option_name)
    model_fold = _fold_text(model)
    if not option_fold or not model_fold:
        return -999.0

    model_tokens = re.findall(r"[a-z0-9]+", model_fold)
    option_tokens = set(re.findall(r"[a-z0-9]+", option_fold))
    if not model_tokens:
        return -999.0

    present = sum(1 for tok in model_tokens if tok in option_tokens)
    if present == 0:
        return -999.0

    score = float(present * 2 - (len(model_tokens) - present) * 4)
    if re.search(rf"\b{re.escape(model_fold)}\b", option_fold):
        score += 10.0

    for qualifier in ("pro", "plus", "max", "mini", "ultra", "lite", "fe"):
        if qualifier in option_tokens and qualifier not in model_tokens:
            score -= 6.0

    # Penalize suffix variants (6A, S21FE, etc.) when user asked plain base model.
    for base_num in re.findall(r"\b(\d+)\b", model_fold):
        if re.search(rf"\b{base_num}[a-z]\b", option_fold) and not re.search(
            rf"\b{base_num}[a-z]\b", model_fold
        ):
            score -= 12.0

    return score


def _resolve_model_variant(item_url: str, model: str) -> dict | None:
    options = _get_item_variation_options(item_url)
    if not options:
        return None

    best = None
    best_score = -999.0
    for option in options:
        score = _score_model_option(str(option.get("name") or ""), model)
        if score > best_score:
            best_score = score
            best = option
    if best is None:
        return None
    if best_score < 2.0:
        return None
    return best


def _enrich_offers_with_model_variant_price(offers: list[dict], model: str) -> None:
    clean_model = normalize_spaces(model)
    if not clean_model or not offers:
        return

    target_count = max(0, VARIANT_ENRICH_MAX_OFFERS)
    if target_count == 0:
        return

    targets = []
    for row in offers:
        url = str(row.get("url") or "")
        if "/itm/" not in url:
            continue
        targets.append(row)
        if len(targets) >= target_count:
            break

    if not targets:
        return

    def _task(entry: dict) -> tuple[dict, dict | None]:
        resolved = _resolve_model_variant(str(entry.get("url") or ""), clean_model)
        return entry, resolved

    with ThreadPoolExecutor(max_workers=min(6, len(targets))) as pool:
        futures = [pool.submit(_task, row) for row in targets]
        for future in as_completed(futures):
            try:
                row, resolved = future.result()
            except Exception:
                continue
            if not resolved:
                continue

            item_id = extract_offer_id(str(row.get("url") or ""))
            price_eur = None
            for variation_id in (resolved.get("variationIds") or []):
                candidate = _fetch_fr_variant_price(str(item_id), str(variation_id))
                if candidate is not None and candidate > 0:
                    price_eur = candidate
                    break

            if price_eur is None:
                fallback_price = float(resolved.get("priceEur") or 0)
                fallback_currency = str(resolved.get("currency") or "").upper()
                if fallback_price > 0 and fallback_currency == "EUR":
                    price_eur = fallback_price

            if price_eur is None or price_eur <= 0:
                continue
            shipping_eur = float(row.get("shippingEur") or 0)
            if str(resolved.get("currency") or "").upper() == "EUR":
                shipping_eur = float(resolved.get("shippingEur") or 0)
            row["priceEur"] = round(price_eur, 2)
            row["shippingEur"] = round(shipping_eur, 2)
            row["totalEur"] = round(row["priceEur"] + row["shippingEur"], 2)
            variant_label = f"Variante: {normalize_spaces(str(resolved.get('name') or ''))}"
            existing = normalize_spaces(str(row.get("conditionText") or ""))
            row["conditionText"] = (
                f"{existing} | {variant_label}" if existing else variant_label
            )


def _search_ebay_via_jina(
    brand: str,
    model: str,
    part_type: str,
    max_price_eur: float | None,
    category: str = "mobile_phone_parts",
    timeout_seconds: int = 24,
) -> list[dict]:
    query = build_query(brand, model, part_type, category=category)
    max_price_param = f"&_udhi={int(max_price_eur)}" if max_price_eur else ""
    category_param = "&_sacat=15032" if category == "mobile_phone_parts" else ""
    source_url = f"{BASE_URL}/sch/i.html?_nkw={quote_plus(query)}&_sop=15&rt=nc{max_price_param}"
    source_url += category_param
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
    brand: str,
    model: str,
    part_type: str,
    max_price_eur: float | None,
    category: str = "mobile_phone_parts",
    timeout_seconds: int = 18,
) -> list[dict]:
    query = build_query(brand, model, part_type, category=category)
    max_price_param = ""
    if max_price_eur is not None and max_price_eur > 0:
        max_price_param = f"&_udhi={int(max_price_eur)}"
    category_param = "&_sacat=15032" if category == "mobile_phone_parts" else ""

    url = (
        f"{BASE_URL}/sch/i.html?_nkw={quote_plus(query)}"
        f"&_sop=15&LH_BIN=1{max_price_param}{category_param}&rt=nc"
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
        offers = _search_ebay_via_jina(
            brand,
            model,
            part_type,
            max_price_eur,
            category=category,
            timeout_seconds=24,
        )

    _enrich_offers_with_model_variant_price(offers, model)

    recent_ids = _fetch_recent_offer_ids(query, max_price_eur, timeout_seconds=20)
    for row in offers:
        row["isRecentlyAdded"] = str(row.get("sourceOfferId") or "") in recent_ids

    return offers[:120]
