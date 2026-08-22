"""
The page a guest is sent when their money lands.

Three separate claims, and the first is the reason the other two exist.

**The link is a credential, and the reference is not one.** `reservations.ref`
is `TB-` plus six characters from a 32-symbol alphabet, produced by
`random.choices` — Mersenne Twister. It is built to be read aloud over a phone,
and a URL that opens somebody's booking has to be built for something else.
`public_token` is 16 bytes from `gen_random_bytes`, and rotating it revokes the
old link.

**The projection is the access control.** The token view says more than the
reference view — price breakdown, folio, cancellation terms — and *less* in one
specific place: it never echoes back the registration a guest filed. A
confirmation gets forwarded to whoever is paying, and whoever is paying is not
owed the traveller's passport number.

**A balance a guest cannot act on is worse than no balance.** See `_amount_due`.
"""
import re

import pytest

from app.api.v1.pms import endpoint
from app.api.v1.pms.schemas import BookingPageOut, PublicStayOut

STAY = {
    "id": "res-1", "ref": "TB-G39J4X", "status": "confirmed",
    "total_paise": 5376000, "paid_paise": 1075200,
    "cancellation_policy": {},
}


def folio(*amounts: int) -> list[dict]:
    return [{"amount_paise": a, "kind": "payment" if a < 0 else "room",
             "description": "", "method": "", "posted_at": "2026-08-22T11:00:00Z"}
            for a in amounts]


# ── what a guest is told they owe ────────────────────────────────────────────

def test_a_stay_that_has_not_started_owes_the_rest_of_its_total():
    """
    The bug this is: `folio_balance` sums a folio holding nothing but the
    deposit, so a guest with ₹43,008 left to pay was shown "₹-10,752" — and the
    check-in page, which renders a balance only when it is above zero, showed
    them nothing at all.
    """
    assert endpoint._amount_due(STAY, folio(-1075200)) == 4300800


def test_an_empty_folio_is_the_same_answer():
    assert endpoint._amount_due(STAY, []) == 4300800


def test_once_the_folio_has_charges_the_folio_is_the_truth():
    """It carries the extras a guest ordered after arriving. The total does not."""
    in_house = {**STAY, "status": "checked_in"}
    # Two nights posted, an airport pickup, the deposit.
    assert endpoint._amount_due(in_house, folio(480000, 480000, 120000, -1075200)) == 4800


def test_a_settled_folio_owes_nothing():
    done = {**STAY, "status": "checked_out"}
    assert endpoint._amount_due(done, folio(1195200, -1195200)) == 0


def test_an_overpaid_folio_reads_as_a_credit_rather_than_a_debt():
    done = {**STAY, "status": "checked_out"}
    assert endpoint._amount_due(done, folio(1000000, -1195200)) == -195200


@pytest.mark.parametrize("status", ["cancelled", "no_show"])
def test_a_cancelled_booking_that_was_never_charged_owes_nothing(status):
    """Its total is a number about a stay that is not happening."""
    assert endpoint._amount_due({**STAY, "status": status}, []) == 0


def test_a_cancelled_booking_that_was_charged_still_owes_it():
    """A retained night is a real charge, and cancelling does not erase it."""
    killed = {**STAY, "status": "cancelled"}
    assert endpoint._amount_due(killed, folio(480000, -1075200)) == -595200


def test_the_amount_due_is_never_a_negative_invitation_to_pay():
    """Before the folio opens, a guest who has overpaid is shown zero, not a
    number that reads as the hotel owing them mid-booking."""
    overpaid = {**STAY, "total_paise": 100000, "paid_paise": 500000}
    assert endpoint._amount_due(overpaid, []) == 0


# ── cancellation terms ───────────────────────────────────────────────────────

def terms(snapshot=None, property_policy=None) -> str:
    return endpoint._cancellation_terms({
        "stay": {"cancellation_policy": snapshot if snapshot is not None else {}},
        "property": {"policies": property_policy or {}},
    })


def test_the_hotels_own_sentence_is_what_a_guest_is_shown():
    assert terms(property_policy={"cancellation": "Free until 48 hours before."}) \
        == "Free until 48 hours before."


def test_the_terms_at_the_time_of_booking_win():
    """A hotel that rewrites its policy has not rewritten anybody's booking."""
    assert terms(snapshot={"cancellation": "Free until 24 hours before."},
                 property_policy={"cancellation": "No refunds."}) \
        == "Free until 24 hours before."


def test_a_hotel_that_has_written_nothing_produces_nothing():
    """No invented default. The page says the hotel has published none."""
    assert terms() == ""


# ── the projection ───────────────────────────────────────────────────────────

def test_the_booking_page_cannot_carry_a_guests_documents():
    """
    Not "does not" — cannot. The field is absent from the model, so no future
    edit to the route can put a passport number on a page whose URL gets
    forwarded to whoever is paying.
    """
    assert "registration" not in BookingPageOut.model_fields
    assert "registrationSubmittedAt" in BookingPageOut.model_fields
    # The check-in view is the one that legitimately needs it back.
    assert "registration" in PublicStayOut.model_fields


def test_the_page_never_reprints_its_own_credential():
    """A page that shows its token is one screenshot away from sharing it."""
    assert "publicToken" not in BookingPageOut.model_fields
    assert "public_token" not in BookingPageOut.model_fields


def test_neither_public_view_carries_an_internal_id():
    for model in (BookingPageOut, PublicStayOut):
        fields = set(model.model_fields)
        assert not fields & {"id", "reservationId", "propertyId", "partnerId",
                             "guestId", "roomId", "guestEmail", "guestPhone"}


def test_a_receipt_line_does_not_name_the_receptionist():
    from app.api.v1.pms.schemas import ReceiptLineOut
    assert "postedByName" not in ReceiptLineOut.model_fields


# ── the token ────────────────────────────────────────────────────────────────

def test_only_a_token_shaped_string_reaches_the_database():
    good = "fa4e84cf92e34af740f6321c44fd9666"
    assert endpoint._TOKEN_SHAPE.fullmatch(good)
    for bad in ("", "x", good[:-1], good + "a", good.upper(),
                # The wildcards. `ilike` would have treated these as patterns,
                # which is how a coupon check was once bypassed in this codebase.
                "%" * 32, "_" * 32, good[:-1] + "%"):
        assert not endpoint._TOKEN_SHAPE.fullmatch(bad), bad


def test_a_token_lookup_is_exact_and_a_reference_lookup_is_not(monkeypatch):
    """
    `ilike` is right for a reference somebody typed and wrong for a token
    nobody types, because it reads `%` and `_` as wildcards.
    """
    seen: list[tuple] = []

    class Q:
        def select(self, *_a, **_k): return self
        def eq(self, col, val): seen.append(("eq", col, val)); return self
        def ilike(self, col, val): seen.append(("ilike", col, val)); return self
        def order(self, *_a, **_k): return self
        def limit(self, _n): return self
        def execute(self): return type("R", (), {"data": []})()

    monkeypatch.setattr(endpoint, "get_db", lambda: type("D", (), {"table": lambda _s, _n: Q()})())

    endpoint._load_public_booking("fa4e84cf92e34af740f6321c44fd9666")
    assert seen[0] == ("eq", "public_token", "fa4e84cf92e34af740f6321c44fd9666")

    seen.clear()
    endpoint._load_public_stay("tb-g39j4x")
    assert seen[0] == ("ilike", "ref", "tb-g39j4x")


def test_rotating_scopes_the_update_to_the_hotel(monkeypatch):
    """A reservation id from another hotel must change nothing."""
    applied: dict = {}

    class Q:
        def update(self, payload): applied["payload"] = payload; return self
        def eq(self, col, val): applied.setdefault("filters", []).append((col, val)); return self
        def execute(self): return type("R", (), {"data": []})()

    monkeypatch.setattr(endpoint, "get_db", lambda: type("D", (), {"table": lambda _s, _n: Q()})())

    assert endpoint._rotate_booking_token("prop-1", "res-1") is None
    assert applied["filters"] == [("id", "res-1"), ("property_id", "prop-1")]
    # A new token, of the right shape, from `secrets` rather than `random`.
    assert re.fullmatch(r"[0-9a-f]{32}", applied["payload"]["public_token"])


def test_a_rotation_produces_a_different_token_every_time(monkeypatch):
    tokens = set()

    class Q:
        def update(self, payload): tokens.add(payload["public_token"]); return self
        def eq(self, *_a): return self
        def execute(self): return type("R", (), {"data": []})()

    monkeypatch.setattr(endpoint, "get_db", lambda: type("D", (), {"table": lambda _s, _n: Q()})())
    for _ in range(50):
        endpoint._rotate_booking_token("prop-1", "res-1")
    assert len(tokens) == 50


# ── the link that gets sent ──────────────────────────────────────────────────

def test_the_confirmation_links_to_the_booking_not_the_check_in_form():
    """
    What a guest wants 26 seconds after paying is the reference, the money and
    the dates. The check-in form is one link on from there.
    """
    from app.config import settings
    from app.services.messaging import service as messaging

    url = messaging.booking_url({"ref": "TB-G39J4X",
                                 "public_token": "fa4e84cf92e34af740f6321c44fd9666"})
    assert url == f"{settings.site_url.rstrip('/')}/b/fa4e84cf92e34af740f6321c44fd9666"
    assert "TB-G39J4X" not in url


def test_a_row_read_without_its_token_still_produces_a_working_link():
    """A projection that did not select the column must not yield a link to
    nowhere."""
    from app.services.messaging import service as messaging

    url = messaging.booking_url({"ref": "TB-G39J4X"})
    assert url.endswith("/checkin/TB-G39J4X")
