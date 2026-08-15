"""
Is this message allowed to say what it says?

Every other agent in this system is stopped from inventing a number by never
being given a way to state one: a price comes back from `get_quote`, an
availability from `search_availability`, and the tool's `instruction` field
tells the model to quote what it was given. That works because the guest is
asking about one stay and the answer is a row.

Marketing breaks it. The model is *writing prose* that will go to two hundred
people, and prose is exactly where "20% off!" appears without anything having
returned 20. There is no tool to route around, because the message body is the
model's output rather than a tool's input.

So the check moves to the boundary: **a campaign body may not assert a number
the campaign's structured offer does not authorise.**

    offer   {"discount_pct": 10, "valid_until": "2026-09-30", "rate_plan": "…"}
    body    "20% off if you book by Friday"
            ── refused: 20 is not the authorised 10, Friday is not a date

That is a blunt instrument and it is meant to be. It cannot understand a
sentence and does not try; it extracts every number that looks like money, a
percentage or a date, and requires each one to be present in the offer. A model
that wants to write persuasive copy can — it simply cannot put a figure in it
that nobody approved.

**What it is not.** It is not a content filter, not a tone check and not a
safety layer. A campaign whose body passes this is not "approved"; it is merely
free of unauthorised claims. A human still approves the campaign, and this runs
before them so they are not the thing catching arithmetic.
"""
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Percentages: "20%", "20 %", "20 percent", "20 per cent".
_PCT = re.compile(r"(\d{1,3})\s*(?:%|percent|per\s?cent)", re.IGNORECASE)

# Money, in the shapes Indian copy actually uses: ₹4,480 · Rs 4480 · INR 4,480.
_MONEY = re.compile(r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE)

# ISO dates, and the day-month forms a model writes when asked for a deadline.
_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

# Absolute claims that are almost always false and are never checkable from an
# offer. Refused by name because each one is a promise the hotel then has to
# keep: "guaranteed" is a legal claim, "last chance" is a scarcity claim nobody
# verified, and "free" is a price of zero that no offer field ever authorises.
_FORBIDDEN = (
    "guaranteed", "guarantee", "risk free", "risk-free", "no questions asked",
    "last chance", "final call", "only today", "expires today",
    "lowest price", "best price", "cheapest", "unbeatable",
    "fully refundable",
)


@dataclass
class Verdict:
    """What was found, and whether the body may be sent."""

    ok: bool
    problems: list[str] = field(default_factory=list)
    # Every claim extracted, whether or not it was allowed. Recorded on the
    # campaign so an argument six weeks later is about a list rather than about
    # what somebody remembers the copy saying.
    claims: dict = field(default_factory=dict)

    def fail(self, problem: str) -> "Verdict":
        self.ok = False
        self.problems.append(problem)
        return self


def _numbers(text: str) -> tuple[set[int], set[str], set[str]]:
    pcts = {int(m) for m in _PCT.findall(text) if m.isdigit()}
    money = {m.replace(",", "") for m in _MONEY.findall(text)}
    dates = set(_ISO_DATE.findall(text))
    return pcts, money, dates


def _authorised(offer: dict) -> tuple[set[int], set[str], set[str]]:
    """
    Every figure the offer permits the copy to state.

    Read leniently on shape and strictly on value: an offer that names
    `discount_pct: 10` authorises 10 and nothing else, but it may spell its
    fields in whichever of these ways the campaign was built with.
    """
    offer = offer or {}

    pcts: set[int] = set()
    for key in ("discount_pct", "discountPct", "percent_off"):
        try:
            if offer.get(key) is not None:
                pcts.add(int(offer[key]))
        except (TypeError, ValueError):
            continue

    money: set[str] = set()
    for key in ("price_paise", "pricePaise", "amount_paise", "from_paise"):
        try:
            if offer.get(key) is not None:
                # Copy is written in rupees; the offer is stored in paise. Both
                # spellings are authorised so "₹4,480" matches 448000 without
                # the model having to know which unit the column uses.
                paise = int(offer[key])
                money.add(str(paise))
                money.add(str(paise // 100))
        except (TypeError, ValueError):
            continue
    for key in ("price_inr", "priceInr", "amount_inr"):
        try:
            if offer.get(key) is not None:
                money.add(str(int(offer[key])))
        except (TypeError, ValueError):
            continue

    dates: set[str] = set()
    for key in ("valid_until", "validUntil", "valid_from", "validFrom",
                "check_in", "checkIn", "check_out", "checkOut"):
        value = offer.get(key)
        if isinstance(value, str) and _ISO_DATE.fullmatch(value):
            dates.add(value)

    return pcts, money, dates


def check(body: str | None, offer: dict | None) -> Verdict:
    """
    Whether this body may be sent for this offer.

    Called before a campaign may leave `draft`, and again in the approval
    revalidation — because the offer can be edited after the copy was written,
    and copy that was true on Tuesday is the thing that goes out on Thursday.
    """
    verdict = Verdict(ok=True)
    text = (body or "").strip()
    if not text:
        return verdict.fail("the campaign has no message body")

    pcts, money, dates = _numbers(text)
    verdict.claims = {
        "percentages": sorted(pcts),
        "amounts": sorted(money),
        "dates": sorted(dates),
    }

    allowed_pcts, allowed_money, allowed_dates = _authorised(offer or {})

    for pct in sorted(pcts - allowed_pcts):
        verdict.fail(
            f"the message claims {pct}% but the campaign's offer authorises "
            + (", ".join(f"{p}%" for p in sorted(allowed_pcts)) if allowed_pcts
               else "no discount at all")
        )

    for amount in sorted(money - allowed_money):
        verdict.fail(
            f"the message states an amount of {amount} which is not in the "
            "campaign's offer — prices come from the offer, not from the copy"
        )

    for when in sorted(dates - allowed_dates):
        verdict.fail(
            f"the message names the date {when}, which the campaign's offer "
            "does not carry — an expiry nobody configured is one the hotel "
            "cannot honour"
        )

    lowered = text.lower()
    for phrase in _FORBIDDEN:
        if phrase in lowered:
            verdict.fail(
                f"the message says {phrase!r}, which is a claim no offer field "
                "can authorise. Say what the offer actually is."
            )

    return verdict


def describe_offer(offer: dict | None) -> str:
    """
    The offer as a line the model is *given*, so it does not have to guess.

    The counterpart to `check`: refusing an invented number is only half a
    system, and the other half is telling the model the real one. Everything
    here comes from the campaign row.
    """
    offer = offer or {}
    parts: list[str] = []

    pcts, money, dates = _authorised(offer)
    if pcts:
        parts.append(f"a discount of {max(pcts)}%")
    if money:
        # The rupee spelling, which is what copy uses.
        rupees = sorted(int(m) for m in money)
        parts.append(f"a price of ₹{rupees[0]:,}")
    if dates:
        parts.append(f"valid until {max(dates)}")

    if offer.get("inclusions"):
        parts.append("including " + ", ".join(str(i) for i in offer["inclusions"][:4]))

    if not parts:
        return (
            "This campaign has no offer attached. Write about the hotel and the "
            "stay — do not state a discount, a price or a deadline of any kind."
        )
    return (
        "The only commercial facts you may state are: " + "; ".join(parts) + ". "
        "Any other figure will be refused by the server."
    )
