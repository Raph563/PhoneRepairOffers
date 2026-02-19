from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import urlparse, urlunparse

AMBIGUOUS_WORDS = {
    "pour pieces",
    "pour piece",
    "hs",
    "lot",
    "defectueux",
    "cass",
    "broken",
    "sans ecran",
}


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def to_ascii_fold(text: str) -> str:
    clean = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in clean if not unicodedata.combining(ch))


def parse_price_to_eur(raw: str) -> float:
    if not raw:
        return 0.0
    text = to_ascii_fold(raw).lower().replace("eur", "").replace("€", "").replace(",", ".")
    m = re.search(r"([0-9]+(?:\.[0-9]{1,2})?)", text)
    if not m:
        return 0.0
    return round(float(m.group(1)), 2)


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def compute_offer_id(source: str, source_offer_id: str, url: str) -> str:
    payload = f"{source}|{source_offer_id}|{canonicalize_url(url)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def compute_rank_score(title: str, total_eur: float) -> float:
    title_fold = to_ascii_fold(title).lower()
    penalty = 0.0
    for word in AMBIGUOUS_WORDS:
        if word in title_fold:
            penalty += 5.0
    return round(total_eur + penalty, 3)


def dedupe_offers(offers: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result: list[dict] = []
    for offer in offers:
        key = f"{offer.get('source')}|{canonicalize_url(str(offer.get('url', '')))}"
        if key in seen:
            continue
        seen.add(key)
        result.append(offer)
    return result
