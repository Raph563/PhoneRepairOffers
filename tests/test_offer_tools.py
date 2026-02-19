from app.services.offer_tools import compute_rank_score, dedupe_offers, parse_price_to_eur


def test_parse_price_to_eur():
    assert parse_price_to_eur("123,45 EUR") == 123.45
    assert parse_price_to_eur("9 € livraison") == 9.0
    assert parse_price_to_eur("no price") == 0.0


def test_rank_score_penalty():
    low = compute_rank_score("Ecran iPhone 12 original", 20)
    high = compute_rank_score("iPhone 12 pour pieces HS", 20)
    assert high > low


def test_dedupe_offers():
    offers = [
        {"source": "ebay", "url": "https://example.com/item/1?abc=1"},
        {"source": "ebay", "url": "https://example.com/item/1"},
        {"source": "leboncoin", "url": "https://example.com/item/1"},
    ]
    out = dedupe_offers(offers)
    assert len(out) == 2
