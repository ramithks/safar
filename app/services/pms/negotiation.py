"""
How far a price may move, and who has to say so.

The discount floor has existed since the first pricing work: `price_stay()`
clamps a requested discount to `properties.settings.max_discount_pct`, which is
what makes it safe to let a model *ask* for 30% off — the worst it can get is
the floor. That was enough while an agent could only ever discount by itself.

Selling needs a second number. A guest who says "another hotel quoted me less"
is asking for something the agent cannot grant and a manager might, and the
honest answer to that is neither "no" nor a silent clamp to the automatic floor
— it is a proposal a human approves. So:

    max_discount_pct           what the agent may grant unattended
    max_approved_discount_pct  the ceiling an approval may authorise
    discount_reasons           what an exception may be filed under

**The approval ceiling defaults to the automatic one.** A property that has
never opened its settings gets no negotiating headroom at all, and
`propose_rate_exception` tells the agent so rather than queuing something no
manager can grant. Defaulting the other way — say, twice the automatic floor —
would have handed every hotel on the platform a discount authority it never
asked for, the day this shipped, discoverable only by reading a bill.

**Both ceilings are enforced in the same place: `price_stay()`.** This module
resolves numbers; it does not apply them. An approved exception raises the
clamp, it does not remove it, so an approval carrying `discount_pct: 90` still
produces a quote at the property's approval ceiling. That is the second layer,
and it exists because the first layer is an argument on an approval row that a
manager pressed a button on without reading.

Nothing here is a table. See migration 16, §4, for why.
"""
import logging
from dataclasses import dataclass, replace

from app.db.client import get_db, run_db
from app.services.pms.pricing import DEFAULT_MAX_DISCOUNT_PCT, resolve_property_limits

logger = logging.getLogger(__name__)

# What a discount may be filed under.
#
# **This vocabulary is the database's, not this module's.**
# `quotes_discount_reason_check` has enumerated it since migration 4, and the
# first draft of this file invented a second, more descriptive one —
# `long_stay`, `price_match`, `goodwill`. It read better and it was wrong: the
# insert failed on the check constraint at the moment a manager pressed Approve,
# which is the worst possible place to discover a vocabulary mismatch.
#
# The existing words cover the sales cases once you read them as what they are:
#
#     volume       a long stay or several rooms
#     loyalty      a guest who has stayed before
#     negotiation  a price argued down, including a competitor match
#     coupon       a campaign the hotel is running
#     manual       everything else, and a human should look at it
#
# `creator` is deliberately not offered to a sales agent: it is a marketing
# mechanism with its own approval path, and an agent filing a negotiated
# discount under it would corrupt the one report that reads it.
QUOTE_REASONS = frozenset({"volume", "negotiation", "coupon", "loyalty", "manual"})

DEFAULT_REASONS = ("volume", "negotiation", "loyalty", "manual")


@dataclass(frozen=True)
class NegotiationPolicy:
    """One property's negotiating rules, as the agent is allowed to see them."""

    auto_max_pct: int
    approved_max_pct: int
    reasons: tuple[str, ...]
    token_pct: int
    # True when this policy is the conservative default because a read failed,
    # rather than because the hotel configured it. Carried so a fallback is
    # observable and not merely safe — see EMERGENCY_DEFAULT.
    degraded: bool = False

    @property
    def negotiable(self) -> bool:
        """Whether an exception is worth proposing at all."""
        return self.approved_max_pct > self.auto_max_pct

    def ceiling_for(self, *, approved: bool) -> int:
        return self.approved_max_pct if approved else self.auto_max_pct


def resolve(settings: dict | None, *, degraded: bool = False) -> NegotiationPolicy:
    """
    Read the policy off `properties.settings`, forgivingly.

    Malformed values fall back rather than raise, for the same reason
    `resolve_property_limits` does: a property whose JSON somebody hand-edited
    should still be able to take a booking, just not a generous one. Every
    fallback here is the conservative direction.
    """
    settings = settings or {}
    auto, token = resolve_property_limits(settings)

    try:
        approved = int(settings.get("max_approved_discount_pct", auto))
    except (TypeError, ValueError):
        approved = auto

    # An approval ceiling below the automatic floor is a contradiction — it would
    # mean a manager may authorise less than the agent already may — and the safe
    # reading is that the property meant no extra headroom, not that it meant to
    # revoke what the agent has.
    approved = max(auto, min(approved, 100))

    raw = settings.get("discount_reasons")
    reasons: tuple[str, ...] = DEFAULT_REASONS
    if isinstance(raw, list):
        # Filtered against the column's own check constraint, not merely
        # cleaned. A hotelier who types 'long_stay' into their settings gets a
        # reason the agent refuses to use, rather than one it accepts and then
        # fails to insert under — which would turn a typo in a JSON field into a
        # 500 at the moment a manager approved a discount.
        cleaned = tuple(
            r for r in (
                str(x).strip().lower().replace(" ", "_")
                for x in raw if isinstance(x, str) and x.strip()
            )
            if r in QUOTE_REASONS
        )
        # An empty list means the property configured its way to no valid
        # reasons, which would make every exception unfileable. Read as "not
        # configured" instead of as a lock nobody could have intended.
        if cleaned:
            reasons = cleaned

    return NegotiationPolicy(
        auto_max_pct=auto,
        approved_max_pct=approved,
        reasons=reasons,
        token_pct=token,
        degraded=degraded,
    )


def _read_settings(property_id: str) -> dict | None:
    rows = (
        get_db().table("properties").select("settings")
        .eq("id", property_id).limit(1).execute().data or []
    )
    return rows[0].get("settings") if rows else None


async def for_property(property_id: str) -> NegotiationPolicy:
    """
    The policy for one property.

    Not cached. A hotelier who lowers their discount ceiling because the agent
    has been too generous expects the next message to obey it, and the read is
    one indexed row on a table already in every relevant query plan.
    """
    try:
        return resolve(await run_db(_read_settings, property_id))
    except Exception:  # noqa: BLE001
        logger.warning("could not read negotiation policy for %s — falling back to "
                       "the conservative default", property_id, exc_info=True)
        # The conservative default, not the property's. A settings read that
        # failed must never widen what the agent may do.
        return resolve({"max_discount_pct": DEFAULT_MAX_DISCOUNT_PCT},
                       degraded=True)


# ═════════════════════════════════════════════════════════════════════════════
# Rules with a floor, and the engine that decides against them
# ═════════════════════════════════════════════════════════════════════════════
#
# Everything above this line is the percentage ceiling that has existed since
# the first pricing work: how *generous* the agent may be. It is a good rule and
# it is only half of what selling needs, because it cannot express the number a
# hotelier actually holds in their head — the price below which they would
# rather lose the booking.
#
#     public   ₹5,000     what the guest is shown
#     target   ₹4,700     where the hotel would like to land
#     floor    ₹4,300     below which the answer is no
#
# `negotiation_rules` holds all three, per rate plan, room type or hotel, most
# specific winning. A hotel that has set nothing keeps exactly the behaviour it
# has today: `resolve_rules` falls back to `properties.settings`, which is why
# the migration deliberately backfilled no rows.
#
# **Three properties of this engine matter more than anything it computes.**
#
# 1. *The model does no arithmetic.* `negotiate()` returns a verdict, a number
#    and a sentence's worth of reason. The LLM's whole job is to say it out loud.
#    Nothing in the tool layer adds, subtracts or compares prices.
#
# 2. *The floor never leaves the server.* No field of `Decision` is the floor.
#    A counter-offer may happen to equal it — that is the §9 example, and telling
#    a guest "I can do ₹4,300" is fine — but there is no field called `floor`,
#    `minimum` or `target` for a projection to leak or a prompt to repeat.
#
# 3. *Every guardrail in §10 is a branch here.* Blackout dates, validity windows,
#    selling days, minimum nights, minimum rooms, sold-out rooms and closed
#    plans are all refusals returned by this function, not instructions in a
#    prompt. A model that is talked into ignoring its instructions still cannot
#    get a number out of this that the hotel did not authorise.

from datetime import date, timedelta  # noqa: E402

from app.services.pms.availability import nightly_for_plan, stay_options  # noqa: E402
from app.services.pms.pricing import date_range, nights_between  # noqa: E402


@dataclass(frozen=True)
class Rules:
    """
    One negotiable thing's rules, already resolved through the scope chain.

    `source` names which scope won, so a hotelier debugging "why won't it go
    lower on the suite" can be told *which* of their rules is binding rather
    than being handed the number.
    """

    enabled: bool
    source: str  # 'rate_plan' | 'room_type' | 'property' | 'settings'
    floor_pct: int | None
    floor_paise: int | None
    target_pct: int | None
    target_paise: int | None
    ceiling_paise: int | None
    auto_pct: int
    approval_pct: int
    valid_from: date | None
    valid_to: date | None
    days_of_week: frozenset[int]
    blackout_dates: frozenset[str]
    min_nights: int
    min_units: int

    @property
    def negotiable(self) -> bool:
        """Whether asking a human could ever produce a better answer."""
        return self.approval_pct > self.auto_pct


def _row_to_rules(row: dict, fallback: NegotiationPolicy) -> Rules:
    """
    One `negotiation_rules` row, with the property's settings behind it.

    A row that leaves `auto_pct` blank has not said the agent may give away
    nothing — it has said nothing about the agent, and the hotel's existing
    ceiling still applies. Falling back rather than defaulting to zero is what
    stops adding a floor to one room type silently revoking that room's
    discretion.
    """
    def _int(key: str) -> int | None:
        value = row.get(key)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    auto = _int("auto_pct")
    approved = _int("approval_pct")
    auto = fallback.auto_max_pct if auto is None else max(0, min(auto, 100))
    approved = fallback.approved_max_pct if approved is None else max(0, min(approved, 100))
    # The same contradiction the property-level resolver settles, settled the
    # same way: an approval ceiling below the automatic one is read as "no extra
    # headroom", never as a revocation of what the agent already has.
    approved = max(auto, approved)

    return Rules(
        enabled=bool(row.get("enabled", True)),
        source=str(row.get("scope") or "property"),
        floor_pct=_int("floor_pct"),
        floor_paise=_int("floor_paise"),
        target_pct=_int("target_pct"),
        target_paise=_int("target_paise"),
        ceiling_paise=_int("ceiling_paise"),
        auto_pct=auto,
        approval_pct=approved,
        valid_from=_as_date(row.get("valid_from")),
        valid_to=_as_date(row.get("valid_to")),
        days_of_week=frozenset(int(d) for d in (row.get("days_of_week") or [])),
        blackout_dates=frozenset(str(d)[:10] for d in (row.get("blackout_dates") or [])),
        min_nights=max(1, _int("min_nights") or 1),
        min_units=max(1, _int("min_units") or 1),
    )


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


# ── where a rule came from ───────────────────────────────────────────────────
#
# **A safe fallback has to be observable, and this one was not.** For weeks every
# call to `_read_rules` raised `AttributeError` in the test fixture and fell
# through to `rules_from_settings` — deliberately, because a failed read must
# never widen what the agent may give away. Nothing broke, nothing failed, and
# the per-room-type and per-rate-plan rules had no test in the suite that
# reached them. It was found in a debugging trace, not by anything watching.
#
# The failure mode this makes visible is worse in production than in a fixture:
# a hotel that has configured a strict floor on its suites, and a database read
# that fails, gets the *hotel-wide* ceiling instead — a safe direction, and
# still not the rules the hotelier set. Silently.
#
# So the source is named, it distinguishes "nobody configured anything" from
# "we could not find out", and it rides out with the decision.
RATE_PLAN_RULE = "rate_plan"          # this plan's own rule
ROOM_TYPE_RULE = "room_type"          # this room's own rule
PROPERTY_RULE = "property"            # a hotel-wide rule row
HOTEL_DEFAULT = "settings"            # no rule row; the hotel's ceiling
EMERGENCY_DEFAULT = "emergency"       # we could not read, and fell back

# The sources that mean the answer is not the one the hotelier configured.
DEGRADED = frozenset({EMERGENCY_DEFAULT})


def rules_from_settings(policy: NegotiationPolicy,
                        source: str = HOTEL_DEFAULT) -> Rules:
    """
    The rules a hotel has when it has configured none.

    Exactly today's behaviour, expressed in the new vocabulary: a percentage
    ceiling, no floor price, no calendar restrictions. Enabled, because a hotel
    whose agent has been negotiating within its ceiling for months must not have
    that switched off by a migration it never saw.
    """
    return Rules(
        enabled=True,
        source=source,
        floor_pct=None,
        floor_paise=None,
        target_pct=None,
        target_paise=None,
        ceiling_paise=None,
        auto_pct=policy.auto_max_pct,
        approval_pct=policy.approved_max_pct,
        valid_from=None,
        valid_to=None,
        days_of_week=frozenset(),
        blackout_dates=frozenset(),
        min_nights=1,
        min_units=1,
    )


def select_rules(rows: list[dict], rate_plan_id: str | None, room_type_id: str | None,
                 fallback: NegotiationPolicy) -> Rules:
    """
    Most specific scope wins: rate plan, then room type, then hotel.

    A *disabled* row still wins its scope. That is the point of it — a hotel
    that has switched negotiation off on one rate plan means that plan not to be
    negotiable, not to fall through to the hotel-wide rule and be negotiable
    after all.
    """
    by_scope = {str(r.get("scope")): r for r in rows}

    plan_row = by_scope.get("rate_plan")
    if plan_row and rate_plan_id and str(plan_row.get("rate_plan_id")) == str(rate_plan_id):
        return _row_to_rules(plan_row, fallback)

    type_row = by_scope.get("room_type")
    if type_row and room_type_id and str(type_row.get("room_type_id")) == str(room_type_id):
        return _row_to_rules(type_row, fallback)

    prop_row = by_scope.get("property")
    if prop_row:
        return _row_to_rules(prop_row, fallback)

    return rules_from_settings(fallback)


def _read_rules(property_id: str, rate_plan_id: str | None, room_type_id: str | None) -> list[dict]:
    """
    Every rule that could apply to this thing, in one query.

    Three `or`-ed predicates rather than three round trips, and filtered by
    `property_id` first so the index does the work. The scope resolution happens
    in Python because it is a precedence rule, and a precedence rule expressed
    as SQL is one nobody can unit-test.
    """
    query = get_db().table("negotiation_rules").select("*").eq("property_id", property_id)
    clauses = ["scope.eq.property"]
    if room_type_id:
        clauses.append(f"room_type_id.eq.{room_type_id}")
    if rate_plan_id:
        clauses.append(f"rate_plan_id.eq.{rate_plan_id}")
    return query.or_(",".join(clauses)).limit(10).execute().data or []


async def rules_for(
    property_id: str,
    *,
    rate_plan_id: str | None = None,
    room_type_id: str | None = None,
    settings: dict | None = None,
) -> Rules:
    """
    The rules binding this quote.

    `settings` may be passed by a caller that has already read the property —
    `create_quote` has — so the common path costs one extra indexed read rather
    than two. A read that fails falls back to the conservative default, the same
    direction `for_property` falls: a failure must never widen what may be given
    away.
    """
    fallback = resolve(settings) if settings is not None else await for_property(property_id)
    if fallback.degraded:
        # The *settings* read failed, so there is no point asking for rule rows
        # against a policy we could not establish. One failure, one honest
        # answer, rather than a rule row applied on top of a ceiling we guessed.
        return rules_from_settings(fallback, EMERGENCY_DEFAULT)
    try:
        rows = await run_db(_read_rules, property_id, rate_plan_id, room_type_id)
    except Exception:  # noqa: BLE001
        # **Named, not just logged.** The fallback direction is unchanged — a
        # read that failed must never widen what may be given away — and now the
        # decision carries the fact that it is a fallback, so a hotel whose
        # suite rules stopped applying is visible rather than merely safe.
        logger.warning("could not read negotiation rules for %s — falling back to "
                       "the hotel ceiling", property_id, exc_info=True)
        return rules_from_settings(fallback, EMERGENCY_DEFAULT)
    return select_rules(rows, rate_plan_id, room_type_id, fallback)


# ── the decision ─────────────────────────────────────────────────────────────


# What a verdict means, and what the agent is allowed to do with it.
#
#   allowed        the guest's number is grantable now. `final_paise` is what the
#                  quote will actually come to — not what they asked for, which
#                  may differ by a rupee or two once an integer percentage has
#                  been applied. The agent quotes `final_paise` and nothing else.
#   counter        the guest's number is not grantable and this one is. The agent
#                  may name `counter_paise` out loud; it is an offer, not a floor.
#   needs_approval a human could grant exactly what was asked. The agent says it
#                  is checking with the hotel and names no number.
#   rejected       no price is available for this request at all, for a reason
#                  that is about the *request* — closed dates, sold out, too
#                  small, negotiation switched off.
VERDICTS = ("allowed", "counter", "needs_approval", "rejected")


@dataclass(frozen=True)
class Decision:
    """
    What the hotel's rules say about one asking price.

    **There is no floor on this object, and that is the design.** Every field
    here is either something the guest may be told or something the agent needs
    to choose a sentence. `counter_paise` may happen to equal the floor — that is
    the §9 example and it is fine to say — but nothing is *labelled* as the
    minimum, so no projection, log line or prompt can leak one.
    """

    verdict: str
    reason: str

    # The published price for this stay, before any negotiation. Always present:
    # it is what the guest was already shown.
    published_total_paise: int
    published_nightly_paise: int

    # What the quote will come to if this is acted on. Set for 'allowed' only.
    final_total_paise: int | None = None
    final_nightly_paise: int | None = None
    # The integer percentage that produces exactly `final_total_paise` through
    # `price_stay`. This is what is handed to `create_quote`, so the number the
    # agent says and the number the quote computes cannot drift apart.
    discount_pct: int | None = None

    # The best price available without asking anybody. Set for 'counter'.
    counter_total_paise: int | None = None
    counter_nightly_paise: int | None = None
    counter_discount_pct: int | None = None

    # Whether proposing an exception could produce a better answer than the one
    # above. A boolean, never a number: "a manager might do better" is safe to
    # act on, "a manager could go to ₹4,100" is the floor with extra steps.
    approval_available: bool = False
    # **Which rule produced this, and whether it is the one the hotelier set.**
    #
    # `rule_source` is one of RATE_PLAN_RULE, ROOM_TYPE_RULE, PROPERTY_RULE,
    # HOTEL_DEFAULT or EMERGENCY_DEFAULT. A hotel that has configured a strict
    # floor on its suites and gets the hotel-wide ceiling instead — because a
    # read failed — is a safe outcome and still the wrong rules, and until this
    # existed the two were indistinguishable in every record we keep.
    #
    # Not a number and not the floor: this is the *name of a scope*, which
    # leaks nothing a guest could use. See the note on this class about why no
    # field here is the floor.
    rule_source: str = HOTEL_DEFAULT

    nights: int = 0
    units: int = 0

    def as_dict(self) -> dict:
        """The projection an agent tool returns. Deliberately not `asdict()`."""
        out = {
            "verdict": self.verdict,
            "reason": self.reason,
            "publishedTotalPaise": self.published_total_paise,
            "publishedNightlyPaise": self.published_nightly_paise,
            "nights": self.nights,
            "units": self.units,
            "approvalAvailable": self.approval_available,
        }
        if self.final_total_paise is not None:
            out["finalTotalPaise"] = self.final_total_paise
            out["finalNightlyPaise"] = self.final_nightly_paise
        if self.counter_total_paise is not None:
            out["counterTotalPaise"] = self.counter_total_paise
            out["counterNightlyPaise"] = self.counter_nightly_paise
        return out


def _pct_for(gross: int, wanted_total: int, floor_total: int | None) -> tuple[int, int]:
    """
    The integer discount percentage that lands closest to `wanted_total`, and the
    total it actually produces.

    **This exists because a percentage cannot express every price.** `price_stay`
    takes an integer percentage; a guest asks for a round number of rupees. On a
    ₹19,999 stay, "₹18,000 please" is 9.995% and there is no integer that gives
    it. Rounding to 10% would produce ₹17,999 — a rupee *below* what was asked,
    which is a rupee below the floor on the request that lands exactly on it.

    So: round to nearest, then check the result against the floor and step back
    one percentage point if it went under. The returned total is what the quote
    will genuinely compute, and it is the number the agent is given to say —
    never the guest's own figure, which may be a rupee away from anything the
    pricing engine can produce.
    """
    if gross <= 0:
        return 0, 0
    wanted = max(0, min(wanted_total, gross))
    pct = round((gross - wanted) * 100 / gross)
    pct = max(0, min(int(pct), 100))
    total = gross - round(gross * pct / 100)
    if floor_total is not None and total < floor_total and pct > 0:
        pct -= 1
        total = gross - round(gross * pct / 100)
    return pct, total


def floor_total_for(gross: int, rules: Rules, nights: int, units: int) -> int | None:
    """
    The lowest total this stay may ever be sold for, or None where the hotel has
    set no floor and only a percentage ceiling applies.

    **Where both a percentage and a price are set, the harder one wins.** They
    say different things — a percentage moves with the season, a price does not —
    and a hotel that has said both means both, so the binding one is whichever is
    higher. Reading it the other way would let a seasonal rate drop a room under
    the absolute price its owner typed in specifically to stop that.
    """
    candidates: list[int] = []
    if rules.floor_pct is not None:
        candidates.append(gross - round(gross * max(0, min(rules.floor_pct, 100)) / 100))
    if rules.floor_paise is not None:
        candidates.append(int(rules.floor_paise) * max(units, 1) * max(nights, 1))
    return max(candidates) if candidates else None


def decide(
    *,
    gross_paise: int,
    requested_total_paise: int,
    rules: Rules,
    nights: int,
    units: int,
) -> Decision:
    """
    The whole engine, as a pure function.

    Separated from the reads above so that every branch is testable without a
    database, and so the dashboard's "what would my AI say" preview and the
    agent's live turn run *the same code* rather than two implementations that
    agree until they do not.
    """
    published_nightly = round(gross_paise / nights / units) if nights and units else 0
    base = dict(
        published_total_paise=gross_paise,
        published_nightly_paise=published_nightly,
        nights=nights,
        units=units,
    )

    if not rules.enabled:
        return Decision(
            verdict="rejected",
            reason="negotiation_closed",
            **base,
        )

    floor = floor_total_for(gross_paise, rules, nights, units)

    # The best the agent may do alone, and the best an approval could reach.
    # Both are clamped by the floor, which is what makes the floor a floor
    # rather than a suggestion: a 40% approval ceiling on a room with a ₹4,300
    # floor cannot produce ₹3,000, whoever presses Approve.
    auto_total = gross_paise - round(gross_paise * rules.auto_pct / 100)
    approved_total = gross_paise - round(gross_paise * rules.approval_pct / 100)
    if floor is not None:
        auto_total = max(auto_total, floor)
        approved_total = max(approved_total, floor)
    approved_total = min(approved_total, auto_total)

    requested = max(0, int(requested_total_paise))

    # Asking for the published price, or more than it, is not a negotiation.
    # Answered as `allowed` at the published rate rather than at the guest's own
    # higher number: a hotel does not charge somebody extra for offering.
    if requested >= gross_paise:
        return Decision(
            verdict="allowed",
            reason="at_published_rate",
            final_total_paise=gross_paise,
            final_nightly_paise=published_nightly,
            discount_pct=0,
            **base,
        )

    if requested >= auto_total:
        pct, total = _pct_for(gross_paise, requested, floor)
        return Decision(
            verdict="allowed",
            reason="within_automatic_authority",
            final_total_paise=total,
            final_nightly_paise=round(total / nights / units) if nights and units else 0,
            discount_pct=pct,
            **base,
        )

    counter_pct, counter_total = _pct_for(gross_paise, auto_total, floor)
    counter_nightly = round(counter_total / nights / units) if nights and units else 0

    if requested >= approved_total:
        # A human could grant exactly this. The agent says it is checking and
        # names no number — including not naming the counter, because offering
        # ₹4,500 while asking a manager about ₹4,300 is how a guest ends up
        # believing they were quoted twice.
        return Decision(
            verdict="needs_approval",
            reason="above_floor_below_automatic",
            counter_total_paise=counter_total,
            counter_nightly_paise=counter_nightly,
            counter_discount_pct=counter_pct,
            approval_available=True,
            **base,
        )

    return Decision(
        verdict="counter",
        reason="below_floor",
        counter_total_paise=counter_total,
        counter_nightly_paise=counter_nightly,
        counter_discount_pct=counter_pct,
        # True only when asking somebody would genuinely help. A hotel whose
        # approval ceiling equals its automatic one has nothing to escalate to,
        # and telling the agent otherwise produces a proposal no manager can
        # grant and a guest kept waiting for it.
        approval_available=approved_total < auto_total,
        **base,
    )


def _eligible(rules: Rules, check_in: date, check_out: date, units: int) -> str | None:
    """
    Whether this request may be negotiated at all. Returns the refusal, or None.

    Every one of these is §10, and every one of them is here rather than in a
    prompt. A model can be talked out of an instruction; it cannot be talked out
    of a function that returns "blackout".
    """
    nights = nights_between(check_in, check_out)
    if nights < rules.min_nights:
        return "below_minimum_nights"
    if units < rules.min_units:
        return "below_minimum_rooms"

    if rules.valid_from and check_in < rules.valid_from:
        return "outside_valid_dates"
    # Compared against the last *night*, not the checkout morning. A rule valid
    # to 31 March covers a stay that ends on 1 April, because nobody sleeps on
    # the night they leave.
    if rules.valid_to and (check_out - timedelta(days=1)) > rules.valid_to:
        return "outside_valid_dates"

    stay = date_range(check_in, check_out)
    if rules.blackout_dates and any(d.isoformat() in rules.blackout_dates for d in stay):
        return "blackout_dates"
    if rules.days_of_week and any(d.isoweekday() not in rules.days_of_week for d in stay):
        return "outside_valid_days"
    return None


async def negotiate(
    *,
    property_id: str,
    rate_plan_id: str,
    check_in: date,
    check_out: date,
    units: int,
    requested_nightly_paise: int | None = None,
    requested_total_paise: int | None = None,
    settings: dict | None = None,
) -> Decision:
    """
    What the hotel's rules say about a guest's asking price.

    This is the function §9 asks for. It does the reading, applies the §10
    guardrails, and hands the arithmetic to `decide()`. It writes nothing and
    holds nothing — a decision is not a quote, and turning one into the other is
    the caller's job, done through `create_quote` so that a negotiated price
    takes stock and expires like every other price this system has ever quoted.

    **The published price comes from the calendar, through the same function the
    booking engine uses.** `nightly_for_plan(units=…)` already resolves seasonal
    rates, derived agency rates and volume tiers, so a negotiation on six rooms
    starts from the six-room price rather than from the rack rate — which is
    what stops a hotel's own group discount being handed out twice.

    A guest's number arrives per night per room far more often than as a stay
    total ("₹4,000 mein ho jayega?"), so both are accepted and the per-night one
    is multiplied here rather than by the model.
    """
    nights = nights_between(check_in, check_out)
    if nights <= 0 or units < 1:
        return Decision(
            verdict="rejected", reason="invalid_stay",
            published_total_paise=0, published_nightly_paise=0, nights=max(nights, 0), units=units,
        )

    resolved = await nightly_for_plan(property_id, rate_plan_id, check_in, check_out, units=units)
    if resolved is None:
        # Closed dates, an unknown plan, or a derived plan whose parent has gone.
        # All three are "there is no price to negotiate", which is a different
        # thing from "your offer is too low" and must not be answered as one.
        return Decision(
            verdict="rejected", reason="no_rate_available",
            published_total_paise=0, published_nightly_paise=0, nights=nights, units=units,
        )
    nightly, _tier = resolved
    gross = sum(p for _, p in nightly) * units

    # Room type, for scope resolution and for the availability check below. Read
    # from the plan rather than taken from the caller: a model that could name
    # the room type could name a different one from the rate plan and negotiate
    # against rules that do not apply to what it is selling.
    plan_rows = await run_db(_read_plan, property_id, rate_plan_id)
    if not plan_rows:
        return Decision(
            verdict="rejected", reason="no_rate_available",
            published_total_paise=gross, published_nightly_paise=round(gross / nights / units),
            nights=nights, units=units,
        )
    room_type_id = str(plan_rows[0]["room_type_id"])

    rules = await rules_for(
        property_id, rate_plan_id=rate_plan_id, room_type_id=room_type_id, settings=settings,
    )

    refusal = _eligible(rules, check_in, check_out, units)
    if refusal:
        return Decision(
            verdict="rejected", reason=refusal,
            published_total_paise=gross,
            published_nightly_paise=round(gross / nights / units),
            nights=nights, units=units, rule_source=rules.source,
        )

    # Negotiating a room nobody can have is worse than refusing to: the guest
    # agrees to a price and then discovers there is no room, which is the one
    # failure a hotel cannot apologise its way out of.
    options = await stay_options(property_id, check_in, check_out, units=units, audience=None)
    available = next(
        (
            plan.units_left
            for opt in options if opt.room_type_id == room_type_id
            for plan in opt.rate_plans if plan.rate_plan_id == rate_plan_id and not plan.closed
        ),
        0,
    )
    if available < units:
        return Decision(
            verdict="rejected", reason="not_enough_rooms",
            published_total_paise=gross,
            published_nightly_paise=round(gross / nights / units),
            nights=nights, units=units,
        )

    if requested_total_paise is not None:
        wanted = int(requested_total_paise)
    elif requested_nightly_paise is not None:
        wanted = int(requested_nightly_paise) * units * nights
    else:
        # No number at all is a legitimate question — "what's your best price?" —
        # and the honest answer is the best the agent may give unattended. Asking
        # for zero and being countered at the automatic floor produces exactly
        # that, through the same branch, with no special case to keep in step.
        wanted = 0

    priced = decide(
        gross_paise=gross,
        requested_total_paise=wanted,
        rules=rules,
        nights=nights,
        units=units,
    )
    # `decide` is pure and knows the rules but not where they came from — the
    # scope resolution happened two frames up. Stamped here, once, so every
    # branch of `decide` carries it without each one having to remember.
    return replace(priced, rule_source=rules.source)


def _read_plan(property_id: str, rate_plan_id: str) -> list[dict]:
    return (
        get_db().table("rate_plans").select("id,room_type_id,audience,is_active")
        .eq("id", rate_plan_id).eq("property_id", property_id)
        .eq("is_active", True).limit(1).execute().data or []
    )
