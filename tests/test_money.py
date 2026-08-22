"""
One booking, one balance, whichever screen you open it on.

These are the cases that made the four separate implementations disagree. Each
one is a real shape a booking passes through, and each one is a shape where at
least two of the old four gave different answers.

The discriminating cases for `amount_due` itself are pinned in
`test_booking_page.py`, through the name the public views call it by. What is
here is the rest of the picture — paid, refunded, and the grouped reading the
dashboard and the front desk use — plus the invariant the whole module exists
for: **every surface, one number.**
"""
import pytest

from app.services.pms import money

# A four-night stay: ₹43,008 total, ₹10,752 taken as a deposit at booking.
BOOKED = {"status": "confirmed", "total_paise": 4300800, "paid_paise": 1075200}


def entries(*pairs) -> list[dict]:
    """`(kind, paise)` → folio rows, in the sign convention the folio stores."""
    return [{"kind": k, "amount_paise": a} for k, a in pairs]


# ── the bug the module was extracted for ────────────────────────────────────

def test_a_deposit_alone_is_not_a_negative_balance():
    """
    The failure, verbatim: a guest with ₹43,008 left to pay was shown ₹-10,752.

    A booking before arrival has a folio holding one thing — the deposit — so
    the folio's *sum* is negative. Three of the four old surfaces showed that
    sum. It is not what the guest owes; it is what nobody has charged yet.
    """
    folio = entries(("payment", -1075200))
    assert money.amount_due(BOOKED, folio) == 3225600

    purse = money.state(BOOKED, folio)
    assert purse.balance_due_paise == 3225600
    assert purse.paid_paise == 1075200
    assert purse.folio_open is False


def test_the_folio_wins_once_it_has_been_opened():
    """
    An in-house guest's extras are on the folio and not on the reservation.

    `max(0, total - paid)` — what the agent and the inbox panel computed —
    cannot see the ₹1,200 breakfast, so the agent would quote ₹4,800 while the
    desk's screen said ₹6,000 for the same booking.
    """
    in_house = {"status": "checked_in", "total_paise": 4300800, "paid_paise": 1075200}
    folio = entries(
        ("room", 4800000), ("tax", 576000), ("addon", 120000), ("payment", -1075200),
    )
    purse = money.state(in_house, folio)

    assert purse.folio_open is True
    assert purse.total_paise == 5496000       # room + tax + the add-on
    assert purse.paid_paise == 1075200
    assert purse.balance_due_paise == 4420800
    # And nothing here is the reservation's own total, which is now stale.
    assert purse.total_paise != in_house["total_paise"]


# ── what counts as money ────────────────────────────────────────────────────

def test_a_write_off_is_not_the_guest_paying():
    """
    An `adjustment` is the hotel forgiving a charge, not the guest settling one.

    Summing every negative entry as "paid" — which the first version of
    `_payments` did — tells a guest they have paid ₹3,600 they never handed
    over. It still reduces what they owe, correctly, because a written-off
    charge is a charge nobody has to pay.
    """
    folio = entries(("room", 1000000), ("adjustment", -360000), ("payment", -200000))
    purse = money.state({"status": "checked_in"}, folio)

    assert purse.paid_paise == 200000          # not 560000
    assert purse.balance_due_paise == 440000   # the write-off did reduce it


def test_a_refund_is_read_off_the_folio_and_not_off_a_column():
    """
    There is no `reservations.refunded_paise`, and pretending to read one would
    have reported zero refunds forever while looking like it had checked.
    """
    folio = entries(("room", 1000000), ("payment", -1000000), ("payment", 300000))
    purse = money.state({"status": "cancelled"}, folio)

    assert purse.refunded_paise == 300000
    assert purse.paid_paise == 1000000
    # The hotel is holding ₹700,000 against a ₹1,000,000 charge.
    assert purse.balance_due_paise == 300000


def test_a_refund_is_not_counted_as_something_the_guest_was_charged_for():
    """
    A refund is a positive folio entry, and the naive `sum(positive)` reading of
    "total" inflates a receipt by exactly the amount just given back — the worst
    possible direction for that error.
    """
    folio = entries(("room", 1000000), ("payment", -1000000), ("payment", 250000))
    assert money.state({"status": "checked_out"}, folio).total_paise == 1000000


# ── the edges ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status", ["cancelled", "no_show"])
def test_a_stay_called_off_before_anyone_charged_owes_nothing(status):
    assert money.amount_due({"status": status, "total_paise": 4300800}, []) == 0


def test_a_balance_is_never_a_negative_invitation_to_pay():
    """
    Before the folio opens, "paid more than the total" is clamped: a guest shown
    a negative balance reads it as an instruction to pay a negative amount.
    """
    overpaid = {"status": "confirmed", "total_paise": 1000000, "paid_paise": 1200000}
    purse = money.state(overpaid, [])

    assert purse.balance_due_paise == 0
    # The fact is not lost, it is carried where it can be acted on.
    assert purse.is_overpaid is True
    assert purse.is_settled is True


def test_an_open_folio_that_has_overpaid_stays_negative():
    """
    The other side of the clamp, and deliberately different.

    Once the folio is open a negative sum means the desk took more than the bill
    — a real refund somebody owes — rather than "the nights have not been
    posted yet". Flattening it to zero would hide money the hotel is holding.
    """
    settled_over = {"status": "checked_out", "total_paise": 1195200, "paid_paise": 1195200}
    assert money.amount_due(settled_over, entries(("room", 1000000),
                                                  ("payment", -1195200))) == -195200


def test_no_folio_at_all_reads_the_reservation():
    assert money.state(BOOKED).balance_due_paise == 3225600
    assert money.state(BOOKED, None).balance_due_paise == 3225600


# ── the grouped reading ─────────────────────────────────────────────────────

def test_the_grouped_row_and_the_entries_give_the_same_answer():
    """
    The invariant that makes the dashboard safe to compute cheaply.

    The arrivals list reads folios in one grouped query rather than one per
    booking. If that shortcut could produce a different number from the entries
    themselves, the receptionist's screen and the guest's receipt would disagree
    — which is the whole failure this module was extracted to end.
    """
    folio = entries(
        ("room", 4800000), ("tax", 576000), ("addon", 120000),
        ("adjustment", -60000), ("payment", -1075200), ("payment", 25000),
    )
    grouped = {
        "charges_paise": 4800000 + 576000 + 120000 - 60000,
        "payments_paise": 1075200,
        "refunds_paise": 25000,
    }
    stay = {"status": "checked_in", "total_paise": 4300800, "paid_paise": 1075200}

    from_entries = money.state(stay, folio)
    from_grouped = money.state(stay, money.rows_from_state(grouped))

    assert from_grouped.balance_due_paise == from_entries.balance_due_paise
    assert from_grouped.paid_paise == from_entries.paid_paise
    assert from_grouped.refunded_paise == from_entries.refunded_paise
    assert from_grouped.total_paise == from_entries.total_paise
    assert money.due_from_state(stay, grouped) == from_entries.balance_due_paise


def test_an_absent_grouped_row_means_the_folio_was_never_opened():
    """
    `folios.get(id)` returns None for a booking with no folio, which is most of
    them. That must read as "before arrival", not as "zero owed".
    """
    assert money.rows_from_state(None) == []
    assert money.due_from_state(BOOKED, None) == 3225600
    assert money.due_from_state(BOOKED, {}) == 3225600


# ── everything is an integer ────────────────────────────────────────────────

def test_nothing_here_ever_becomes_a_float():
    """
    One paisa of drift is enough for a guest to stop trusting the rest of a
    receipt, and floats are how it gets there.
    """
    folio = entries(("room", 333333), ("tax", 1), ("payment", -111111))
    purse = money.state({"status": "checked_in"}, folio)

    for value in (purse.total_paise, purse.paid_paise,
                  purse.refunded_paise, purse.balance_due_paise):
        assert isinstance(value, int)
