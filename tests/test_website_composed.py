"""
A website built from the hotel's own records, and the rule that makes the
button safe to press twice.

**Why there is no model in this path.** `services/marketing/creatives.py` makes
the argument for ad copy; a website is the stronger case, because it is the last
thing a guest reads before paying. A model handed "harry, bhiwani, 12 rooms"
writes about a rooftop pool, and unlike an invented *price* — which
`claims.py` catches — an invented amenity has no digit in it. So every sentence
`website_compose` can produce is a template filled from a column somebody typed
into their own dashboard, and these tests assert the properties that follow from
that: nothing appears that the facts did not contain, and nothing that has no
facts behind it draws at all.

The tests drive `compose` and `inventory` directly with fabricated facts. They
touch no database, because what is being tested is the composition rather than
the nine reads that feed it.
"""
import pytest

from app.services.pms import website, website_compose as wc


def facts(**overrides) -> dict:
    base = {
        "property": {
            "id": "p1", "partner_id": "pa1", "slug": "harry-bhiwani", "name": "harry",
            "property_type": "hotel", "address": "12 Circular Road", "city": "bhiwani",
            "state": "Haryana", "country": "IN", "pincode": "127021",
            "lat": 28.79, "lon": 76.13,
            "check_in_time": "14:00:00", "check_out_time": "11:00:00",
            "total_rooms": 12, "policies": {"smoking": "no"},
        },
        "rooms": [
            {"id": "r1", "name": "Deluxe Room", "description": "A quiet room at the back.",
             "amenities": ["Free WiFi", "AC", "TV"], "photos": ["u1.jpg"],
             "max_occupancy": 2, "bed_type": "Queen", "total_units": 8,
             "children_allowed": "yes"},
            {"id": "r2", "name": "Family Suite", "description": "",
             "amenities": ["wifi", "Air conditioning", "Parking"], "photos": [],
             "max_occupancy": 4, "bed_type": "Twin", "total_units": 4,
             "children_allowed": "yes"},
        ],
        "plans": [
            {"room_type_id": "r1", "base_price_paise": 320000, "meal_plan": "CP",
             "refundable": True, "cancellation_cutoff_hours": 48,
             "cancellation_policy": None},
        ],
        "services": [],
        "experiences": [],
        "meals": [{"meal": "breakfast", "availability": "included", "price_paise": None,
                   "starts_at": "07:30:00", "ends_at": "10:30:00"}],
        "times": [],
        "marketing": {"usp": "Family-run since 1998", "best_for": ["business trips"]},
        "partner": {"phone": "+919812345678", "email": "stay@harry.in"},
        "draft": {},
    }
    return {**base, **overrides}


def bare() -> dict:
    """A hotel that has told us its name and nothing else."""
    return {
        "property": {"id": "p2", "partner_id": "pa1", "slug": "x", "name": "New Hotel",
                     "property_type": "homestay", "city": None, "address": None,
                     "check_in_time": None, "check_out_time": None, "policies": {},
                     "lat": None, "lon": None, "total_rooms": None, "pincode": None},
        "rooms": [], "plans": [], "services": [], "experiences": [], "meals": [],
        "times": [], "marketing": {}, "partner": {}, "draft": {},
    }


# ── nothing is claimed that the hotel did not say ───────────────────────────

def test_nothing_the_hotel_did_not_say_appears_anywhere():
    """
    The property that justifies having no model here.

    Every word of every generated field, checked against the things a page
    about a hotel is tempted to promise. A hotel with no pool must not have the
    word "pool" anywhere on its site — not in the about text, not as a
    highlight, not in an FAQ answer.
    """
    draft = wc.compose(facts())
    written = " ".join([
        draft["headline"], draft["tagline"], draft["about"],
        " ".join(h["label"] for h in draft["highlights"]),
        " ".join(draft["amenities"]),
        " ".join(f"{f['q']} {f['a']}" for f in draft["faq"]),
        draft["seo"].get("title", ""), draft["seo"].get("description", ""),
    ]).lower()

    for absent in ("pool", "spa", "gym", "shuttle", "rooftop", "beach",
                   "restaurant", "michelin", "award", "star hotel"):
        assert absent not in written, f"composed a page claiming {absent!r}"


def test_a_hotel_with_no_facts_still_gets_a_true_page():
    draft = wc.compose(bare())
    assert draft["headline"]
    assert draft["about"].startswith("New Hotel is a homestay.")
    # No check-in time was configured, so the page does not state one.
    assert "check-in" not in draft["about"].lower()
    assert draft["faq"] == []


def test_a_band_with_nothing_behind_it_is_switched_off():
    """
    The difference between a generated page and a template with a name in it.

    `harry` shipped with all eight sections switched on and content for none of
    them — a home page of headings over empty stripes.
    """
    on = {s["id"] for s in wc.compose(bare())["sections"] if s["enabled"]}
    assert "gallery" not in on
    assert "offers" not in on
    assert "faq" not in on
    assert "rooms" not in on
    assert "reviews" not in on
    # What it does have.
    assert "hero" in on
    assert "about" in on


def test_reviews_are_never_switched_on_because_nothing_supplies_them():
    """No review source is connected, so the band must never draw an empty one."""
    for found in (facts(), bare()):
        sections = {s["id"]: s["enabled"] for s in wc.compose(found)["sections"]}
        assert sections["reviews"] is False


# ── the facts it does use ───────────────────────────────────────────────────

def test_the_faq_answers_from_the_rows_the_agent_reads():
    draft = wc.compose(facts())
    answers = {f["q"]: f["a"] for f in draft["faq"]}
    assert answers["What time is check-in?"] == "Check-in is from 2 pm."
    assert answers["What time is check-out?"] == "Check-out is by 11 am."
    assert "included in the room rate" in answers["Is breakfast available?"]
    # Resolved through `policies.cancellation_terms`, not restated from the
    # column — a page that disagrees with the refund calculation is a chargeback.
    assert "2 days" in answers["What is your cancellation policy?"]


def test_a_hotel_without_breakfast_is_not_asked_about_breakfast():
    draft = wc.compose(facts(meals=[]))
    assert not any("breakfast" in f["q"].lower() for f in draft["faq"])
    assert "breakfast" not in draft["about"].lower()
    assert not any(h["icon"] == "breakfast" for h in draft["highlights"])


def test_one_amenity_however_many_ways_the_hotel_spelt_it():
    """
    Two rooms typed "Free WiFi" and "wifi"; a third said "AC" where another
    said "Air conditioning". Six strings, four things.
    """
    amenities = wc.compose(facts())["amenities"]
    assert sum(1 for a in amenities if "wifi" in a.lower().replace("-", "")) == 1
    assert sum(1 for a in amenities if a.lower() in ("ac", "air conditioning")) == 1
    # The hotelier's own words survive — we recognise, we do not rename.
    assert "Free WiFi" in amenities


def test_the_meta_description_is_never_cut_mid_word():
    seo = wc.compose(facts())["seo"]
    assert len(seo["description"]) <= 180
    assert not seo["description"].endswith("-")
    assert seo["description"].rstrip().endswith((".", "!", "?", "…"))


def test_highlights_never_promise_a_rate_nobody_guarantees():
    """
    "Best rate guaranteed" is the one line a hotel could not honour — nothing
    in this system checks an OTA's price. The true half is said instead.
    """
    labels = [h["label"] for h in wc.compose(facts())["highlights"]]
    assert "Book direct with the hotel" in labels
    assert not any("guarantee" in l.lower() for l in labels)
    assert len(labels) <= 5


# ── pressing the button twice ───────────────────────────────────────────────

def test_a_field_the_hotelier_edited_is_no_longer_ours_to_overwrite():
    """
    The rule that makes Generate safe to press again after uploading photos.
    """
    first = wc.compose(facts())
    assert "headline" in first["generatedFields"]

    # What `website._normalise` does when the builder saves a headline: the
    # field leaves the claim list, because a human has touched it.
    edited = {**first, "headline": "Harry's — a family hotel in Bhiwani"}
    edited["generatedFields"] = [f for f in first["generatedFields"] if f != "headline"]

    second = wc.compose(facts(draft=edited))
    assert second["headline"] == "Harry's — a family hotel in Bhiwani"
    # Everything still ours is rebuilt.
    assert second["about"] == first["about"]


def test_force_is_the_start_again_and_takes_the_edits_back():
    first = wc.compose(facts())
    edited = {**first, "headline": "Mine"}
    edited["generatedFields"] = [f for f in first["generatedFields"] if f != "headline"]
    assert wc.compose(facts(draft=edited), force=True)["headline"] == first["headline"]


def test_an_answer_composed_from_a_fact_that_is_gone_does_not_survive():
    """
    A hotel that stops serving breakfast must not keep an FAQ saying it does.
    The answer was ours; when its fact disappears, so does it.
    """
    with_breakfast = wc.compose(facts())
    assert any("breakfast" in f["q"].lower() for f in with_breakfast["faq"])

    after = wc.compose(facts(draft=with_breakfast, meals=[]))
    assert not any("breakfast" in f["q"].lower() for f in after["faq"])


def test_the_normaliser_releases_a_field_the_builder_saved():
    """
    The half of the rule that lives in `website._normalise` — asserted here
    because the two halves are only correct together.
    """
    existing = {"headline": "Composed", "about": "Composed",
                "generatedFields": ["headline", "about"]}
    out = website._normalise({"headline": "Written by the owner"}, existing)
    assert out["generatedFields"] == ["about"]
    # And a save that touches neither leaves the claim intact.
    out2 = website._normalise({"tagline": "x"}, existing)
    assert set(out2["generatedFields"]) == {"headline", "about"}


# ── the gallery, and the photograph at the top ──────────────────────────────

def test_the_hero_is_never_a_bathroom():
    """
    A gallery ordered by what a hotel's home page opens on. Uploading in the
    order the phone happened to hold them is why this ordering exists.
    """
    draft = wc.compose(facts(draft={"gallery": [
        {"url": "bath.jpg", "category": "bathroom"},
        {"url": "front.jpg", "category": "exterior"},
        {"url": "pool.jpg", "category": "pool"},
    ]}))
    assert draft["heroImageUrl"] == "front.jpg"
    assert [p["url"] for p in draft["gallery"]] == ["front.jpg", "pool.jpg", "bath.jpg"]


def test_a_hero_whose_photograph_was_deleted_is_re_picked():
    draft = wc.compose(facts(draft={
        "heroImageUrl": "gone.jpg",
        "gallery": [{"url": "front.jpg", "category": "exterior"}],
    }))
    assert draft["heroImageUrl"] == "front.jpg"


def test_room_photographs_are_not_copied_into_the_gallery():
    """
    They belong to the room, and the rooms band already draws them. A copy
    means a hotel that replaces a room photo still has the old one on its page.
    """
    draft = wc.compose(facts())
    assert draft["gallery"] == []
    # The hero may still *fall back* to one, because a page with a picture on
    # it beats a page with a coloured rectangle.
    assert draft["heroImageUrl"] == "u1.jpg"


# ── what the owner is shown before pressing the button ──────────────────────

def test_the_inventory_counts_rows_rather_than_scoring_the_hotel():
    found = wc.inventory(facts())
    assert found["roomTypes"] == 2
    assert found["rooms"] == 12
    assert found["fromPricePaise"] == 320000
    assert found["hasAddress"] is True
    # Named absences, never a blocker — one room photograph is thin.
    assert "photographs" in found["thin"]


def test_a_hotel_with_nothing_is_told_what_is_thin_and_still_offered_a_page():
    found = wc.inventory(bare())
    assert found["roomTypes"] == 0
    assert set(found["thin"]) == {"photographs", "room descriptions", "published rates"}


# ── sections, across versions ───────────────────────────────────────────────

def test_a_row_written_before_a_section_existed_still_lists_every_section():
    """
    `sections_for` projects through the closed list, so an old row does not
    hand the builder a shorter set of toggles than the one it draws.
    """
    old_row = {"sections": [{"id": "hero", "enabled": True},
                            {"id": "rooms", "enabled": False}]}
    projected = website.sections_for(old_row)
    assert [s["id"] for s in projected] == list(website.SECTIONS)
    assert projected[0] == {"id": "hero", "enabled": True}
    assert {s["id"]: s["enabled"] for s in projected}["rooms"] is False
    # A section it has never heard of arrives on, because a band only draws
    # when it has something to show.
    assert {s["id"]: s["enabled"] for s in projected}["faq"] is True


def test_an_older_builder_cannot_switch_off_sections_it_has_never_heard_of():
    """
    A save from a tab open since before the FAQ existed sends eight ids. Read
    as "the other five are off", it would delete the FAQ from a live page
    because somebody fixed a typo.
    """
    existing = {"sections": [{"id": s, "enabled": True} for s in website.SECTIONS]}
    out = website._normalise(
        {"sections": [{"id": "hero", "enabled": True}, {"id": "rooms", "enabled": False}]},
        existing,
    )
    by_id = {s["id"]: s["enabled"] for s in out["sections"]}
    assert by_id["rooms"] is False        # what the client actually said
    assert by_id["faq"] is True           # what it never mentioned
    assert by_id["gallery"] is True


# ── the storage layer's allow-list ──────────────────────────────────────────

def test_the_new_bands_are_stored_and_bounded():
    out = website._normalise({
        "highlights": [{"icon": "wifi", "label": "Free Wi-Fi"},
                       {"icon": "nonsense", "label": "Something"},
                       {"icon": "pool", "label": "   "}],
        "faq": [{"q": "Q", "a": "A"}, {"q": "", "a": "orphan"}],
        "offers": [{"title": "Stay 3, pay 2", "detail": "d", "terms": None}],
        "seo": {"title": "t" * 200, "description": "d" * 400},
        "design": {"font": "editorial", "buttons": "wobbly", "layout": "wide"},
    }, {})

    assert out["highlights"] == [
        {"icon": "wifi", "label": "Free Wi-Fi"},
        # An unknown icon becomes a known one rather than a blank square.
        {"icon": "rate", "label": "Something"},
    ]
    assert out["faq"] == [{"q": "Q", "a": "A"}]
    assert len(out["seo"]["title"]) == 70
    assert len(out["seo"]["description"]) == 180
    assert out["design"] == {"font": "editorial", "buttons": "rounded", "layout": "wide"}


def test_a_client_cannot_claim_authorship_of_a_field_it_wrote():
    """
    `generatedAt` and `generatedFields` decide what the next generate may
    overwrite. They are the server's, and `_normalise` is the client's door.
    """
    out = website._normalise(
        {"generatedFields": ["headline", "about"], "generatedAt": "2020-01-01T00:00:00Z"},
        {},
    )
    assert "generatedAt" not in out
    assert not out.get("generatedFields")
