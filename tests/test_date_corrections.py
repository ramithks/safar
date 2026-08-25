"""
"Actually 25th to 28th" — which nights are those, and are we sure?

**Deliberately narrow, and the narrowness is the point.** A date parser that
tries hard is one that quietly books a guest into the wrong month. The rule is
that a date is read only when reading it cannot be wrong in a way the guest
would not immediately notice; everything else becomes a question.

**Today is an argument, never a lookup.** Every relative date is computed from
*the hotel's* business date, and `date_facts.read` has no default for it — a
caller cannot forget the timezone because there is nothing to forget it to. The
pipeline passes `clock.abusiness_date(property_id)`.

Three confidences, and they are different things to say to a guest:

    explicit    a month or a full date was named
    inferred    the next 25th, the next Friday — right almost always, and the
                model is told to say them back
    derived     computed from a length and a check-in already agreed
"""
from datetime import date

import pytest

from app.services.messaging import session
from app.services.pms import date_facts
from tests.fake_supabase import FakeDb

# A Tuesday, so every weekday assertion below has an unambiguous answer.
TODAY = date(2026, 8, 25)
CONVERSATION = "c0c0c0c0-0001-4001-8001-000000000001"


def read(message, check_in=None):
    return date_facts.read(message, TODAY, check_in=check_in)


class TestTheDatesItReads:
    @pytest.mark.parametrize("message,check_in,check_out,confidence", [
        # spelled out
        ("actually 2026-09-25 to 2026-09-28", date(2026, 9, 25), date(2026, 9, 28),
         date_facts.EXPLICIT),
        ("25 September to 28 September", date(2026, 9, 25), date(2026, 9, 28),
         date_facts.EXPLICIT),
        ("September 25 to September 28", date(2026, 9, 25), date(2026, 9, 28),
         date_facts.EXPLICIT),
        ("25 sept se 28 sept tak", date(2026, 9, 25), date(2026, 9, 28),
         date_facts.EXPLICIT),
        # a month that has already passed this year rolls forward
        ("3 March to 6 March", date(2027, 3, 3), date(2027, 3, 6),
         date_facts.EXPLICIT),
        # a day of the month, resolved to its next occurrence
        ("actually 25th to 28th", date(2026, 8, 25), date(2026, 8, 28),
         date_facts.INFERRED),
        ("2nd to 5th", date(2026, 9, 2), date(2026, 9, 5), date_facts.INFERRED),
        # weekdays, from the hotel's today
        ("check in Friday, checkout Monday", date(2026, 8, 28), date(2026, 8, 31),
         date_facts.INFERRED),
        # a length against a check-in already agreed
        ("make it 3 nights instead", date(2026, 9, 10), date(2026, 9, 13),
         date_facts.DERIVED),
        ("teen raat kar do", date(2026, 9, 10), date(2026, 9, 13),
         date_facts.DERIVED),
        # a date and a length together
        ("25 September ko aaunga, 3 nights", date(2026, 9, 25), date(2026, 9, 28),
         date_facts.EXPLICIT),
    ])
    def test_it_reads(self, message, check_in, check_out, confidence):
        anchor = date(2026, 9, 10) if confidence == date_facts.DERIVED else None
        fact = read(message, check_in=anchor)
        assert fact.check_in == check_in, f"{message!r} → {fact.basis}"
        assert fact.check_out == check_out
        assert fact.confidence == confidence

    def test_a_weekday_said_on_that_weekday_means_next_week(self):
        """
        TODAY is a Tuesday. "Tuesday to Thursday" cannot mean today, because a
        guest saying it is planning, not arriving in the next ten minutes.
        """
        fact = read("Tuesday to Thursday")
        assert fact.check_in == date(2026, 9, 1)

    def test_it_records_how_it_got_there(self):
        """
        Provenance. "Two dates named outright" and "days of the month, resolved"
        are different claims, and the session carries which one it was.
        """
        assert "outright" in read("25 September to 28 September").basis
        assert "next such dates" in read("25th to 28th").basis
        assert "already agreed" in read("3 nights", check_in=date(2026, 9, 1)).basis


class TestWhatItRefusesToRead:
    def test_a_slash_date_is_undecidable_and_says_so(self):
        """
        5/9 is 5 September in India and May 9th to half the internet. Nothing in
        the sentence decides it, and a wrong month is a guest arriving four
        weeks late.
        """
        fact = read("5/9 se 9/9 tak")
        assert not fact.usable
        assert "write the month out" in fact.ask

    def test_but_an_unambiguous_slash_date_is_not_refused_for_nothing(self):
        """
        25/9 has a day above twelve, so only one reading exists. Refusing it
        would be conservatism for its own sake — though it is still not *read*
        here, because the rest of the sentence has to carry it.
        """
        assert read("25/9 to 28/9").ask is None

    def test_backwards_dates_that_are_pinned_are_a_typo_we_cannot_resolve(self):
        """
        Both ends spelled out, and the second before the first. One of the two
        numbers is wrong and there is no way to tell which, so swapping them
        silently would be picking a month for the guest.
        """
        fact = read("2026-09-28 to 2026-09-25")
        assert not fact.usable
        assert "wrong way round" in fact.ask

    def test_but_days_of_the_month_chain_forward_instead(self):
        """
        **My first draft expected "28th to 25th" to be refused, and chaining the
        resolution changed the right answer.**

        Each end is now resolved from the one before it, so "28th to 25th" is
        28 August to 25 September — a long stay, which is unusual and entirely
        real. Refusing it would break a genuine booking to protect against a
        typo whose consequence, a month-long quote, is the most visible thing
        on the page.
        """
        fact = read("28th to 25th")
        assert fact.usable
        assert fact.check_in == date(2026, 8, 28)
        assert fact.check_out == date(2026, 9, 25)

    def test_a_request_to_change_with_no_dates_in_it_asks(self):
        for message in ("dates change kar do", "can we change the dates",
                        "tarikh badal do", "reschedule please"):
            fact = read(message)
            assert not fact.usable
            assert "which nights" in fact.ask.lower(), message

    def test_a_length_with_nothing_to_measure_from_asks(self):
        fact = read("make it 3 nights instead")
        assert not fact.usable
        assert "arrive" in fact.ask

    def test_one_date_alone_asks_for_the_other(self):
        fact = read("check in on 25 September")
        assert not fact.usable
        assert "check out" in fact.ask

    def test_an_implausibly_long_stay_is_a_misreading(self):
        fact = read("2026-09-01 to 2027-06-01")
        assert not fact.usable
        assert "very long stay" in fact.ask

    @pytest.mark.parametrize("message", [
        "breakfast included hai?", "kitna lagega?", "2 log hain",
        "Garden Standard chahiye", "", "   ",
    ])
    def test_a_sentence_with_no_dates_in_it_reads_nothing(self, message):
        fact = read(message)
        assert not fact.usable
        assert fact.ask is None, f"{message!r} produced a question it should not"


class TestTheHotelsTimezone:
    def test_relative_dates_move_with_the_hotels_today(self):
        """
        The whole reason `today` is an argument. The same sentence resolves to
        different nights at a hotel whose day has already turned over.
        """
        in_goa = date_facts.read("25th to 28th", date(2026, 8, 26))
        assert in_goa.check_in == date(2026, 9, 25), (
            "on the 26th, 'the 25th' is next month")
        assert date_facts.read("25th to 28th", date(2026, 8, 24)).check_in == \
            date(2026, 8, 25)

    def test_there_is_no_default_today(self):
        """
        A caller that forgot the timezone would resolve against the server's
        day, so there is nothing to forget it to.
        """
        with pytest.raises(TypeError):
            date_facts.read("25th to 28th")


# ── through the session, which is what production calls ─────────────────────

QUOTED = {
    "check_in": "2026-09-10", "check_out": "2026-09-14",
    "adults": 2, "units": 2, "room_type_id": "rt-deluxe",
    "rate_plan_id": "rp-cp", "quote_id": "q-1", "stage": "quoted",
    "negotiation": {"asked": 4000}, "payment": "pending", "language": "hinglish",
}


@pytest.fixture
def db(monkeypatch):
    fake = FakeDb()
    fake.seed("conversations", {"id": CONVERSATION, "guest_profile": dict(QUOTED)})
    monkeypatch.setattr("app.services.messaging.session.get_db", lambda: fake)
    return fake


def profile(db) -> dict:
    return db.rows("conversations")[0]["guest_profile"]


class TestACorrectionInvalidatesWhatDependedOnIt:
    def test_new_dates_take_the_quote_the_negotiation_and_the_payment(self, db):
        """
        §2's explicit list. Every one of these was true of nights the guest has
        just stopped asking about.
        """
        session.merge(CONVERSATION, session.patch_from(
            [], "actually 25 September to 28 September", today=TODAY))
        now = profile(db)
        assert now["check_in"] == "2026-09-25"
        assert now["check_out"] == "2026-09-28"
        assert "quote_id" not in now
        assert "negotiation" not in now
        assert "payment" not in now
        assert "stage" not in now

    def test_the_room_survives_and_is_marked_for_re_checking(self, db):
        """
        A guest who moves their dates has not changed their mind about the room.
        Its *availability* on the new nights is another matter.
        """
        session.merge(CONVERSATION, session.patch_from(
            [], "25 September to 28 September", today=TODAY))
        now = profile(db)
        assert now["room_type_id"] == "rt-deluxe"
        assert now["room_needs_recheck"] is True

    def test_the_guests_language_survives(self, db):
        session.merge(CONVERSATION, session.patch_from(
            [], "25 September to 28 September", today=TODAY))
        assert profile(db)["language"] == "hinglish"

    def test_the_same_dates_again_invalidate_nothing(self, db):
        """
        §2's other half. A guest restating what they already said is the
        conversation confirming itself, not a correction.
        """
        session.merge(CONVERSATION, session.patch_from(
            [], "10 September to 14 September", today=TODAY))
        now = profile(db)
        assert now["quote_id"] == "q-1", "a restated date threw away the quote"
        assert now["negotiation"] == {"asked": 4000}
        assert now["stage"] == "quoted"

    def test_a_length_correction_moves_only_the_checkout(self, db):
        """
        "Make it 3 nights instead" keeps the arrival they already agreed and
        moves the departure — and the quote priced for four nights goes.
        """
        session.merge(CONVERSATION, session.patch_from(
            [], "make it 3 nights instead", today=TODAY, known=profile(db)))
        now = profile(db)
        assert now["check_in"] == "2026-09-10"
        assert now["check_out"] == "2026-09-13"
        assert "quote_id" not in now

    def test_an_unreadable_correction_changes_no_dates(self, db):
        """
        The safety property. A sentence we could not read must leave the stay
        exactly as it was — the alternative is a stay half-moved to a month
        nobody named.
        """
        session.merge(CONVERSATION, session.patch_from(
            [], "5/9 se 9/9 tak", today=TODAY))
        now = profile(db)
        assert now["check_in"] == "2026-09-10", "an ambiguous date moved the stay"
        assert now["quote_id"] == "q-1"
        assert "write the month out" in now["dates_unclear"]

    def test_and_the_question_reaches_the_prompt(self, db):
        session.merge(CONVERSATION, session.patch_from(
            [], "dates change kar do", today=TODAY))
        told = session.describe({"guest_profile": profile(db)})
        assert "not clear" in told and "do not guess at a month" in told

    def test_an_inferred_date_is_flagged_for_saying_back(self, db):
        session.merge(CONVERSATION, session.patch_from(
            [], "actually 2nd to 5th", today=TODAY))
        now = profile(db)
        assert now["check_in"] == "2026-09-02"
        assert now["dates_need_confirming"] is True
        told = session.describe({"guest_profile": now})
        assert "say the two dates back" in told

    def test_a_spelled_out_date_is_not(self, db):
        session.merge(CONVERSATION, session.patch_from(
            [], "2 September to 5 September", today=TODAY))
        assert "dates_need_confirming" not in profile(db)

    def test_a_caller_with_no_clock_reads_no_dates(self, db):
        """
        The behaviour before this existed, preserved for every caller that has
        outcomes but no hotel timezone to resolve against.
        """
        patch = session.patch_from([], "actually 25th to 28th")
        assert "check_in" not in patch
