"""
What StayOS keeps, and what the hotel receives.

StayOS charges 4% of the booking amount. That number covers the payment gateway
and every other platform charge, and before this module it existed nowhere in
the codebase — not as a column, not as a constant, not as a comment. Every
figure a hotelier had ever been shown about what they would be paid was either
absent or arrived at with a calculator by somebody on a call.

**One function, and every surface uses it.** The same failure `money.py` was
written to end — four screens with four different answers to "what is the
balance" — is available here in a worse form, because a settlement figure that
disagrees with a payout is not a confusing screen, it is a dispute. Booking,
payments, the receipt, the dashboard, settlement and the owner's report all read
`breakdown()`.

**The rate is basis points and the arithmetic is integers.** 400 bps is 4%. A
percentage column forces the day somebody negotiates 3.5% into a float, and a
float has no business near money — `0.045 * 3584000` is not what you think it
is. Everything here is `int`, and the one division rounds once, at a defined
point, exactly as `price_stay()` does.

**The fee is on the booking total, tax included.** That is what the guest's card
is charged and therefore what the gateway takes its cut of; charging 4% of a
pre-tax figure while paying gateway fees on a post-tax one is a margin that
quietly goes negative on high-GST bookings. Stated here because it is a
commercial decision that looks like an implementation detail.

**Nothing here is ever shown to a guest.** §33: the fee and the payout are the
platform's business and the hotel's. A guest who is told a hotel receives ₹34,406
of their ₹35,840 has been handed a negotiating position nobody meant to give
them.
"""
from dataclasses import dataclass

# 400 basis points. The default every partner is created with, and the fallback
# when a partner row cannot be read — deliberately the standard rate rather than
# zero, because a failed read that silently stops charging is a bug nobody
# notices until the month-end reconciliation.
DEFAULT_FEE_BPS = 400

# One hundred per cent, in basis points. A rate above this would pay the hotel a
# negative amount, which is not a fee.
_BPS_DENOMINATOR = 10_000


@dataclass(frozen=True)
class FeeBreakdown:
    """
    One booking's split, in integer paise.

    `total_paise` is what the guest owes. `fee_paise` is what StayOS keeps.
    `payout_paise` is what the hotel is owed, and it is `total - fee` by
    construction rather than by a second calculation — the two must sum back to
    the total exactly, and the only way to guarantee that is to derive one from
    the other.
    """

    total_paise: int
    fee_bps: int
    fee_paise: int
    payout_paise: int

    @property
    def fee_pct(self) -> float:
        """For display only. Never multiply anything by this."""
        return self.fee_bps / 100

    def as_dict(self) -> dict:
        return {
            "totalPaise": self.total_paise,
            "feeBps": self.fee_bps,
            "feePaise": self.fee_paise,
            "payoutPaise": self.payout_paise,
        }


def normalise_bps(bps: int | None) -> int:
    """
    A usable rate, whatever arrived.

    Missing, malformed or out of range all fall back to the standard rate rather
    than raising. The same reasoning as `resolve_property_limits`: a partner row
    somebody hand-edited should not be able to stop a booking being taken — but
    unlike the discount ceiling, the conservative direction here is the *default*
    and not zero. Falling back to no fee would make a data error look like a free
    month.
    """
    try:
        value = int(bps)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_FEE_BPS
    if value < 0 or value > _BPS_DENOMINATOR:
        return DEFAULT_FEE_BPS
    return value


def breakdown(total_paise: int, fee_bps: int | None = None) -> FeeBreakdown:
    """
    Split one booking total into the platform's share and the hotel's.

        fee    = round(total × bps / 10000)
        payout = total − fee

    Rounding happens once. A booking of ₹35,840 at 400 bps is a fee of ₹1,433.60
    exactly, which rounds to ₹1,433.60 → 143360 paise with no remainder; a
    booking of ₹1,000.13 does have one, and it goes to the platform or the hotel
    depending on the round, never to nobody.

    A negative or zero total produces a zero fee rather than a negative one. A
    refunded booking is not a booking StayOS pays the hotel for the privilege of
    having taken.
    """
    total = max(int(total_paise or 0), 0)
    bps = normalise_bps(fee_bps)
    fee = round(total * bps / _BPS_DENOMINATOR)
    # Guards the pathological case rather than trusting the arithmetic: a rate of
    # exactly 10000 bps must produce a payout of zero and not of −1.
    fee = min(max(fee, 0), total)
    return FeeBreakdown(
        total_paise=total,
        fee_bps=bps,
        fee_paise=fee,
        payout_paise=total - fee,
    )


def snapshot_columns(total_paise: int, fee_bps: int | None) -> dict:
    """
    The three columns a reservation carries, ready to insert.

    Written at creation and never again. §37: a hotel renegotiated to 3% next
    year must not find that last year's settled bookings have quietly changed
    what they were worth. Nothing in this codebase recomputes these — that is
    the whole point of storing them, and the reason there is no `refresh` here
    for somebody to call by accident.
    """
    split = breakdown(total_paise, fee_bps)
    return {
        "platform_fee_bps": split.fee_bps,
        "platform_fee_paise": split.fee_paise,
        "payout_paise": split.payout_paise,
    }
