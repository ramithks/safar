"""
What a stay costs, what has been paid, and what is still owed — computed once.

Every surface in StaySuite quotes money at a guest or at a hotelier: the Instagram
agent, the booking confirmation, the receipt, `/b/{token}`, the check-in page,
the inbox session panel and the partner dashboard's reservation list. Before
this module there were **four** different answers to "what is the balance", and
which one a guest saw depended on which screen they opened.

    /b/{token}, /checkin/{ref}   _amount_due(stay, folio)      correct
    booking-engine confirm       rpc folio_balance             negative before arrival
    the agent, the inbox panel   max(0, total - paid)          blind to folio extras
    the dashboard                rpc folio_balance             negative before arrival

The first of those was written *because* the second was wrong: a guest with
₹43,008 left to pay was shown "₹-10,752", because a booking that has not started
has a folio holding exactly one thing — the deposit they paid — and its sum is
negative. That fix landed on two of the four surfaces and the other two kept the
bug.

**So the rule lives here and nowhere else.** Not because four copies are untidy,
but because a guest reading ₹28,672 on Instagram and ₹-10,752 on the receipt for
one booking has no way to know which is real, and neither does the receptionist
they ring about it.

The rule itself, in one sentence: *the folio is the truth once the folio has
been opened, and the reservation is the truth before then.*

Both halves matter. The folio carries the extras a guest ordered after arriving
and the reservation total does not, so an in-house guest must be read from the
folio. And a folio with no charges on it yet is not a statement that the guest
owes nothing — it is a statement that nobody has posted the room nights, which
`post_room_nights` does at check-in — so before then the number that means
something is total minus paid.

Everything is integer paise. There is no float in this module and there must
never be one: `0.1 + 0.2` is how a receipt comes to disagree with a folio by one
paisa, and one paisa is enough for a guest to stop trusting the rest of it.
"""
from dataclasses import dataclass

# A stay that was called off before anyone charged for it owes nothing, whatever
# its total says. Kept as a set rather than inlined because three call sites
# used to each have their own opinion about whether `no_show` counted.
TERMINAL_UNCHARGED = frozenset({"cancelled", "no_show"})


@dataclass(frozen=True)
class Money:
    """
    One booking's financial state, in the vocabulary the brief asks for.

    `total`, `paid`, `refunded` and `balance_due` are the four numbers every
    surface is allowed to show. Anything computing a fifth from them — a
    frontend subtracting, a model doing arithmetic in prose — is the thing this
    module exists to make unnecessary.
    """

    total_paise: int
    paid_paise: int
    refunded_paise: int
    balance_due_paise: int
    # True once the folio has real charges on it, which is what decides which
    # of the two readings above was used. Surfaced so a caller can say "as at
    # check-in" rather than guessing.
    folio_open: bool

    @property
    def is_settled(self) -> bool:
        return self.balance_due_paise == 0

    @property
    def is_overpaid(self) -> bool:
        """
        More has been taken than was owed.

        Distinct from settled on purpose: a refund is owed and somebody has to
        act. `balance_due_paise` never goes negative — a negative balance reads
        to a guest as an instruction to pay a negative amount — so the fact has
        to be carried separately rather than inferred from the sign.
        """
        return self.paid_paise > self.total_paise > 0

    def as_dict(self) -> dict:
        return {
            "totalPaise": self.total_paise,
            "paidPaise": self.paid_paise,
            "refundedPaise": self.refunded_paise,
            "balancePaise": self.balance_due_paise,
        }


def display(paise) -> str:
    """
    Money as somebody reads it. `716800` → `₹7,168`.

    Here, in the module that owns money, because the interesting caller is the
    AI employee. Tool results speak paise and a guest reads rupees, and until
    this existed the conversion happened *in the model*: `totalPaise: 1920000`
    went into the context and the model was left to divide by a hundred. On a
    live replay it divided by a thousand and told a guest ₹1,920 for a stay
    costing ₹19,200. The reply guardrail caught it — the number was backed by
    nothing — so the cost was a handoff rather than a ruinous quote, which is
    the guardrail working and not a reason to keep asking a language model to
    do arithmetic on money.

    Rounded to the rupee, because a hotel quotes in rupees and "₹7,168.00" on
    Instagram reads like a machine. Anything unreadable comes back as an em
    dash rather than raising: a formatter is never the right place to fail a
    turn.
    """
    try:
        return f"₹{int(paise) / 100:,.0f}"
    except (TypeError, ValueError):
        return "—"


def _charges(folio: list[dict]) -> int:
    """
    Every positive line. The switch `amount_due` turns on, kept exactly as the
    function it was extracted from computed it — including a refund, which is
    positive. That is a rounding error in a predicate ("has this folio been
    opened at all"), and changing it would change `amount_due`'s answer on
    cases the existing tests pin.
    """
    return sum(int(e.get("amount_paise") or 0) for e in folio or []
               if int(e.get("amount_paise") or 0) > 0)


def _stay_charges(folio: list[dict]) -> int:
    """
    What the stay has cost so far: room, tax, add-ons, adjustments.

    Distinct from `_charges` because a refund is a positive entry and is not
    something the guest was charged for. Reporting it as part of the total
    would inflate a receipt by exactly the amount the hotel had just given
    back, which is the worst possible direction for that error.
    """
    return sum(int(e.get("amount_paise") or 0) for e in folio or []
               if (e.get("kind") or "") != _MONEY_KIND)


# The folio's kinds, as `folio_entries.kind` actually uses them today:
# `room`, `tax`, `addon` and `adjustment` are what the stay costs, and `payment`
# is money moving. Only the last is money.
_MONEY_KIND = "payment"


def _payments(folio: list[dict]) -> int:
    """
    What the guest has actually handed over, as a positive number.

    Two things are load-bearing here and neither is obvious.

    **The sign flips.** The folio is a running balance, not a ledger of
    receipts, so a payment is stored negative. Flipping it at the one place it
    is read is why `paid` and `balance_due` cannot end up disagreeing about
    which direction money moved.

    **Only `payment` counts.** The first version of this summed every negative
    entry, which quietly included `adjustment` — a manager writing ₹3,600 off a
    disputed charge. That is the hotel forgiving money, not the guest paying it,
    and counting it as paid would tell a guest they had settled a bill nobody
    settled. It still reduces `balance_due`, correctly, because `amount_due`
    sums the whole folio; it must not appear as `paid`.
    """
    return -sum(int(e.get("amount_paise") or 0) for e in folio or []
                if (e.get("kind") or "") == _MONEY_KIND
                and int(e.get("amount_paise") or 0) < 0)


def _refunds(folio: list[dict]) -> int:
    """
    Money given back, as a positive number.

    A refund has no kind of its own: it is a `payment` entry with a positive
    amount, reversing one that was negative. Derived rather than stored so that
    it cannot drift from the folio it is a reading of — and so that adding a
    `refund` kind later changes one line here rather than four surfaces.
    """
    return sum(int(e.get("amount_paise") or 0) for e in folio or []
               if (e.get("kind") or "") == _MONEY_KIND
               and int(e.get("amount_paise") or 0) > 0)


def amount_due(stay: dict, folio: list[dict] | None) -> int:
    """
    What this guest still owes, in the sense a guest means it. Never negative.

    Preserved exactly as `endpoint._amount_due` computed it — this is a move,
    not a rewrite, and the tests that pinned the discriminating cases still pin
    them. The other three surfaces are being brought to this answer.
    """
    entries = folio or []
    if _charges(entries) > 0:
        # Deliberately *not* clamped, unlike the branch below. An open folio
        # that sums negative means the guest has overpaid — the desk took a
        # card for the full stay after a deposit, say — and that is a refund
        # somebody owes, not a zero. The pre-arrival branch clamps because
        # there the same negative number means "the room nights have not been
        # posted yet", which is not money owed to anyone.
        return sum(int(e.get("amount_paise") or 0) for e in entries)
    if (stay.get("status") or "") in TERMINAL_UNCHARGED:
        return 0
    return max(0, int(stay.get("total_paise") or 0) - int(stay.get("paid_paise") or 0))


def state(stay: dict, folio: list[dict] | None = None) -> Money:
    """
    The whole money picture for one reservation.

    `folio` is optional because two callers legitimately do not have one — the
    agent answering "payment hogyi" reads a projection of `reservations` and
    should not pay for a folio read to answer a question about a deposit. Passing
    None means "the folio has not been opened", which is true for every booking
    before check-in and is exactly the case where the reservation columns are
    the right source anyway.

    Passing a folio that *has* been opened and getting a different answer from
    `reservations.paid_paise` is not a contradiction to resolve: the folio is
    later and knows about the extras, so it wins. That is the same precedence
    `amount_due` applies, stated once for both fields.
    """
    entries = folio or []
    total = int(stay.get("total_paise") or 0)
    folio_open = _charges(entries) > 0

    if folio_open:
        total = _stay_charges(entries)
        paid = _payments(entries)
    else:
        paid = int(stay.get("paid_paise") or 0)

    return Money(
        total_paise=total,
        paid_paise=paid,
        # Only the folio records a refund. `reservations` has no
        # `refunded_paise` column, and inventing a read of one would have
        # reported zero refunds forever while looking like it had checked.
        refunded_paise=_refunds(entries),
        balance_due_paise=amount_due(stay, entries),
        folio_open=folio_open,
    )


# ── the grouped reading ──────────────────────────────────────────────────────


def rows_from_state(folio: dict | None) -> list[dict]:
    """
    A `folio_states` row, in the shape the functions above read.

    The arrivals list, the dashboard and the front desk agent all read folios in
    one grouped query rather than one per booking — N+1 against the folio is how
    the screen a receptionist reloads all shift gets slow enough to be worked
    around. That grouped row is four totals, and everything above takes entries.

    So this reconstitutes the smallest list of entries that sums to the same
    thing. Deliberately a translation rather than a second rule: the arithmetic
    still happens in one place, and a dashboard row cannot disagree with the
    guest's own receipt because one of them added up differently.
    """
    if not folio:
        return []
    rows: list[dict] = []
    charges = int(folio.get("charges_paise") or 0)
    payments = int(folio.get("payments_paise") or 0)
    refunds = int(folio.get("refunds_paise") or 0)
    if charges:
        rows.append({"kind": "room", "amount_paise": charges})
    if payments:
        rows.append({"kind": _MONEY_KIND, "amount_paise": -payments})
    if refunds:
        rows.append({"kind": _MONEY_KIND, "amount_paise": refunds})
    return rows


def due_from_state(stay: dict, folio: dict | None) -> int:
    """`amount_due` for a caller holding a grouped `folio_states` row."""
    return amount_due(stay, rows_from_state(folio))


# ── every paise a model can see, beside the words for it ────────────────────

_SUFFIX = "Paise"


def _spoken_name(key: str) -> str:
    """`balancePaise` → `balance`, `totalPaise` → `total`."""
    return key[: -len(_SUFFIX)] or "amount"


def with_display(payload, *, depth: int = 0):
    """
    Add a display sibling for every paise figure in a tool result.

    **Why this is one function at the boundary rather than a line in each tool.**
    Thirty guardrail-blocked production runs were traced to the model formatting
    money itself, and the shape was always the same: a tool returned
    `balancePaise: 3440640` and nothing that said `₹34,406`, so the model divided
    by a hundred and wrote the paise out as decimals — *"₹34,406.40"*. The figure
    was right. It was the *rendering* the model had to invent, and inventing a
    rendering is one keystroke from inventing a number.

    `get_quote` was given display strings by hand after the first of these. The
    other money-returning tools were not, and a survey found `frontdesk` with ten
    paise fields and no display calls at all, `sales` with four. Fixing them one
    at a time would leave the next tool somebody writes in exactly the same
    state, which is the §10 lesson about one regex in a different room.

    So it happens once, in `registry.execute`, on the way out. Every tool that
    exists is covered and so is every tool that will.

    Three rules, each of which matters:

      * a sibling that already exists is left alone, so a tool that formats its
        own money — a group deposit, a cancellation quote — keeps its wording;
      * only integers are converted, because a float in a paise field is already
        a bug and quietly rendering it would hide one;
      * nested structures are walked, because `get_stay_options` carries its
        money two levels down inside `options[].ratePlans[]` and that is exactly
        where the model was reaching.
    """
    if depth > 6:
        return payload
    if isinstance(payload, list):
        return [with_display(item, depth=depth + 1) for item in payload]
    if not isinstance(payload, dict):
        return payload

    out = {key: with_display(value, depth=depth + 1) for key, value in payload.items()}
    for key, value in list(payload.items()):
        if not key.endswith(_SUFFIX) or not isinstance(value, int) or isinstance(value, bool):
            continue
        spoken = _spoken_name(key)
        if spoken in out:
            continue
        out[spoken] = display(value)
    return out
