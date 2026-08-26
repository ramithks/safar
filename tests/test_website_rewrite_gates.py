"""
The one place a model is allowed near a hotel's public words, and the three
things standing between its answer and the page.

`website_compose` keeps a model out of the composition entirely — templates
filled from columns, no exceptions. This module is the deliberate exception:
"make it warmer" over text that already exists. The narrowness is the safety
argument, and these gates are what make it narrow rather than merely intended
to be.

**The gate that matters is the second one.** An invented *price* is caught by
`claims.py` and by a hotelier proofreading. An invented *amenity* has no digit
in it, reads perfectly, and is a promise a guest arrives expecting the hotel to
keep — which somebody discovers at a front desk in Bhiwani rather than in a
code review.

The model itself is never called here. What is tested is `check`, which decides
what may be shown; a test that had to reach Vertex to exercise a safety
property is a test nobody runs.
"""
import pytest

from app.services.pms.website_rewrite import check

SOURCE = (
    "harry is a hotel in Bhiwani. There are 12 rooms across 2 room types. "
    "Breakfast is included, served from 7:30 am. Check-in is from 2 pm."
)


# ── what a rephrasing is allowed to be ──────────────────────────────────────

@pytest.mark.parametrize("candidate", [
    "Harry is a 12-room hotel in Bhiwani, spread over 2 room types. "
    "Breakfast is included and served from 7:30 am; check-in opens at 2 pm.",
    "A 12-room hotel in Bhiwani. Breakfast included from 7:30 am, check-in from 2 pm.",
    "harry is a hotel in Bhiwani with 12 rooms in 2 types. Breakfast is included "
    "from 7:30 am. You can check in from 2 pm.",
])
def test_a_faithful_rephrasing_is_allowed(candidate):
    ok, why = check(candidate, SOURCE)
    assert ok, why


# ── and what it is not ──────────────────────────────────────────────────────

@pytest.mark.parametrize("candidate,because", [
    ("harry is a hotel in Bhiwani with 12 rooms and a rooftop pool.",
     "a pool nobody mentioned"),
    ("harry is a hotel in Bhiwani. There are 12 rooms and a free airport shuttle.",
     "a shuttle nobody mentioned"),
    ("harry is a hotel in Bhiwani with 12 rooms, a restaurant and a spa.",
     "two facilities nobody mentioned"),
    ("harry is a hotel in Bhiwani with 12 rooms and sea views.",
     "a view nobody mentioned"),
])
def test_a_rewrite_may_not_invent_a_facility(candidate, because):
    ok, why = check(candidate, SOURCE)
    assert not ok, f"allowed a rewrite claiming {because}"
    assert "mentioned" in (why or "")


@pytest.mark.parametrize("candidate", [
    "harry is a hotel in Bhiwani. Rooms from ₹1,800 a night, breakfast included.",
    "Breakfast is included, served from 6 am. Check-in is from 2 pm.",
    "harry is a 5 star hotel in Bhiwani with 12 rooms.",
    "harry is a hotel in Bhiwani. 20% off for early bookings.",
])
def test_a_rewrite_may_not_invent_a_figure(candidate):
    """
    Every digit, not just the ones with a currency symbol. "Check-in from 6 am"
    is a fact nobody gave it and has no ₹ anywhere near it.
    """
    ok, why = check(candidate, SOURCE)
    assert not ok
    assert "number" in (why or "")


@pytest.mark.parametrize("phrase", [
    "Lowest price guaranteed.", "The cheapest rooms in Bhiwani.",
    "Book now — last chance!", "Fully refundable, no questions asked.",
])
def test_a_rewrite_may_not_make_a_promise_the_original_did_not(phrase):
    """
    The absolute claims `claims.py` refuses in an ad. A promise that is
    unsendable in a campaign is unpublishable on a home page for the same
    reason — the hotel has to keep it.
    """
    ok, why = check(f"harry is a hotel in Bhiwani with 12 rooms. {phrase}", SOURCE)
    assert not ok
    assert "promise" in (why or "")


def test_a_rewrite_may_not_quietly_become_an_essay():
    ok, why = check("harry is a hotel in Bhiwani. " * 20, SOURCE)
    assert not ok
    assert "longer" in (why or "")


def test_an_empty_answer_is_refused_rather_than_saved_as_a_blank_page():
    assert check("   ", SOURCE)[0] is False


# ── the figures the source itself already committed to ──────────────────────

def test_the_source_authorises_its_own_numbers_however_they_are_punctuated():
    """
    "₹3,200" and "3200" are the same commitment. A rewrite that reformats a
    price the hotelier already published is rephrasing, not inventing.
    """
    source = "Rooms from ₹3,200 a night."
    assert check("From ₹3200 per night.", source)[0] is True
    assert check("From ₹3,300 per night.", source)[0] is False


def test_a_hotel_that_has_a_pool_may_have_its_pool_sentence_rewritten():
    """
    The facility gate is "named in the candidate and not in the source", not a
    ban on the word — otherwise a hotel with a pool could never describe it.
    """
    source = "The pool is open from 7 am to 8 pm."
    ok, why = check("Our pool is open daily, 7 am to 8 pm.", source)
    assert ok, why
