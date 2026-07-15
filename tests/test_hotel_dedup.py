"""
Cross-provider hotel dedup: the same hotel listed by multiple sources collapses to
one row whose PRICE + LINK come from the cheapest bookable provider (so a budget
traveller isn't shown a pricier duplicate, and the price matches the link), while
the richest provider's metadata (subscores, savings, social proof, reviews) is
unioned onto it so no scraped signal is lost.

Regression for the Udaipur dump finding: the same hotel appears rich in Agoda and
bare (but sometimes cheaper) in EaseMyTrip.
"""
from app.services.hotels import _deduplicate_by_name


def test_cheapest_bookable_represents_and_absorbs_metadata():
    agoda_rich = {
        "name": "Lake Pichola Hotel", "price_per_night": 7400,
        "booking_url": "https://agoda/x",
        "subscores": {"overall": 8.3, "location": 8.9}, "review_score": 8.3,
        "social_proof": "Booked twice today", "savings": 3342, "photos": ["a1", "a2"],
    }
    emt_cheap = {
        "name": "Lake  Pichola  Hotel!", "price_per_night": 6800,   # note spacing/punct differ
        "booking_url": "https://emt/y", "photos": ["e1"],
    }
    out = _deduplicate_by_name([agoda_rich, emt_cheap])
    assert len(out) == 1
    rep = out[0]
    # price + link come from the cheapest BOOKABLE provider (consistent pair)
    assert rep["price_per_night"] == 6800
    assert rep["booking_url"] == "https://emt/y"
    # ...but the richer provider's metadata is absorbed
    assert rep["subscores"]["location"] == 8.9
    assert rep["review_score"] == 8.3
    assert rep["social_proof"] == "Booked twice today"
    assert rep["savings"] == 3342
    # photos are unioned (both providers')
    assert "a1" in rep["photos"] and "e1" in rep["photos"]


def test_bookable_beats_richer_nonbookable():
    rich_nobook = {  # no booking_url / no price → not bookable, but richer
        "name": "Grand X", "review_score": 9.1, "star_rating": 5,
        "photos": ["p1", "p2", "p3"], "amenities": ["a", "b", "c"], "address": "Main St",
    }
    thin_book = {"name": "Grand X", "price_per_night": 3000, "booking_url": "https://b/x"}
    out = _deduplicate_by_name([rich_nobook, thin_book])
    assert len(out) == 1
    rep = out[0]
    assert rep["booking_url"] == "https://b/x"          # the bookable one represents
    assert rep["review_score"] == 9.1                   # rich metadata still absorbed
    assert rep["address"] == "Main St"


def test_distinct_hotels_are_not_merged():
    a = {"name": "Hotel Alpha", "price_per_night": 1000, "booking_url": "https://a"}
    b = {"name": "Hotel Beta", "price_per_night": 1200, "booking_url": "https://b"}
    out = _deduplicate_by_name([a, b])
    assert {h["name"] for h in out} == {"Hotel Alpha", "Hotel Beta"}


def test_winner_keeps_its_own_price_never_borrows_cheaper_metadata_price():
    # Two bookable duplicates: cheaper wins outright; the pricier one only lends
    # metadata, never its price.
    pricey = {"name": "Zed", "price_per_night": 9000, "booking_url": "https://p",
              "review_score": 9.0}
    cheap = {"name": "Zed", "price_per_night": 5000, "booking_url": "https://c"}
    out = _deduplicate_by_name([pricey, cheap])
    assert out[0]["price_per_night"] == 5000
    assert out[0]["booking_url"] == "https://c"
    assert out[0]["review_score"] == 9.0
