"""
Withdrawing marketing consent, and the ordinary hotel messages that are not that.

`is_stop_request` had no tests. It was written to be generous — "an opt-out the
system does not recognise is worse than one it over-recognises" — and the
generosity was implemented as *any stop word inside any message under sixty
characters*. In a hotel, that turned out to catch:

    "AC band kar do"            turn the air conditioning off
    "extra bed hata do"         take the extra bed out
    "photo mat bhejo"
    "is there a bus stop nearby?"
    "we had to stop at the airport, reaching late"

Each one silently and permanently opted the guest out of that hotel's
marketing, and the last of them was the example in the function's own docstring
of a message it handled *correctly*. Nothing was guest-visible — the reply still
goes out — which is why it survived: the only symptom is an audience that
quietly shrinks.

Both directions are pinned here, because a fix in this file can fail in two
opposite ways and only one of them is ever noticed.
"""
import pytest

from app.services.marketing import consent


# ── the opt-outs, which must always work ─────────────────────────────────────

@pytest.mark.parametrize("message", [
    "stop", "STOP", "Stop.", "  stop  ",
    "please stop", "stop please", "stop bhai", "kindly stop",
    "unsubscribe", "UNSUBSCRIBE", "please unsubscribe me from these",
    "unsub", "opt out", "opt-out", "optout",
    "remove me", "delete me from your list", "no more messages",
    # Both apostrophes and the bare spelling. The curly one is what a phone
    # keyboard produces, and none of the three matched before.
    "don't message me", "don’t message me", "dont message me",
    "do not message me again", "don't text me",
    "stop messaging me", "leave me alone",
    "band karo", "band kar do", "mat bhejo", "hata do",
    "message mat bhejo",
])
def test_a_guest_asking_us_to_stop_is_heard(message):
    assert consent.is_stop_request(message) is True


# ── the hotel messages, which must not ───────────────────────────────────────

@pytest.mark.parametrize("message", [
    # The five that were live defects.
    "AC band kar do",
    "extra bed hata do",
    "photo mat bhejo",
    "is there a bus stop nearby?",
    "we had to stop at the airport, reaching late",
    # And the shape of the rule: an ambiguous word inside a sentence.
    "non-stop flight se aa rahe hain",
    "light band kar do please",
    "purana booking hata do aur naya karo",
])
def test_an_ordinary_request_to_a_hotel_is_not_an_opt_out(message):
    assert consent.is_stop_request(message) is False


def test_prose_is_never_an_opt_out_however_it_is_worded():
    """
    The length bound, which is the second line of defence rather than the first.
    Past sixty characters somebody is telling a story.
    """
    long_message = (
        "we were going to unsubscribe from the other hotel's list but yours "
        "has been genuinely useful so far"
    )
    assert len(long_message) > consent._MAX_STOP_CHARS
    assert consent.is_stop_request(long_message) is False


def test_nothing_is_not_an_opt_out():
    assert consent.is_stop_request(None) is False
    assert consent.is_stop_request("") is False
    assert consent.is_stop_request("   ") is False
    assert consent.is_stop_request("👍") is False


def test_the_two_vocabularies_do_not_overlap():
    """
    A phrase in both lists would be matched by the loose rule and the strict one
    at once, which makes the strict one decorative and the next person's fix
    ineffective in a way that is hard to see.
    """
    assert not set(consent._STOP_PHRASES) & set(consent._STOP_ALONE)


def test_no_phrase_that_must_stand_alone_is_a_substring_of_a_free_one():
    """
    "stop" standing alone and "stop messaging" matched anywhere are consistent
    only because the second is longer than the first. If a phrase from the
    alone-list were ever a superstring of a free one, the free rule would fire
    first and the restriction would do nothing.
    """
    for alone in consent._STOP_ALONE:
        assert not any(free in alone for free in consent._STOP_PHRASES)
