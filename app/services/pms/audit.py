"""
Who changed the floor price.

`admin_audit_log` records what TravBuddy staff do *to* a hotel. Nothing recorded
what a hotel does to itself, and §42 names exactly the fields where that matters:
prices, the negotiation floor, B2B and group rates, payment rules, cancellation
terms, team permissions. Those are the changes nobody remembers making and
everybody notices a month later.

**One row per changed field, not per request.** A hotelier asking "when did our
minimum drop to ₹3,800" wants that field's history, not a diff of a form
submission with eleven untouched values in it. `diff()` is what turns a payload
into those rows, and it is deliberately the only way in — a caller that had to
name each field itself would eventually forget one, and the one it forgot would
be the one somebody later needed.

**Recording a change must never fail the change.** Every write here swallows its
own exception and logs. An audit row that cannot be written is a gap in a
history; an audit row that raises is a hotelier unable to save their rates
because a logging table is unhappy, which is a strictly worse outcome and the
opposite of what an audit trail is for.
"""
import logging

from app.db.client import get_db, run_db

logger = logging.getLogger(__name__)

# Only what §42 asks for.
#
# The temptation is to log everything, and it is wrong: a table that records a
# room's photo caption alongside its floor price is one nobody scrolls through,
# and an audit log nobody reads is an audit log that is not doing its job. These
# are the fields where money or access changes hands.
SENSITIVE_FIELDS: frozenset[str] = frozenset({
    # money
    "base_price_paise", "price_paise", "extra_bed_paise", "crib_paise",
    "fixed_price_paise", "discount_pct", "derive_discount_pct",
    "floor_paise", "floor_pct", "target_paise", "target_pct", "ceiling_paise",
    "auto_pct", "approval_pct",
    "commission_pct", "credit_limit_paise", "platform_fee_bps",
    "deposit_pct", "token_pct", "max_discount_pct", "max_approved_discount_pct",
    "allocation_units", "min_units", "max_units",
    # terms
    "cancellation", "cancellation_policy", "cancellation_cutoff_hours",
    "refundable", "payment_due_days", "blackout_dates",
    "valid_from", "valid_to", "days_of_week", "enabled",
    # access
    "role", "permissions", "property_ids", "status", "channels",
})


def _stringify(value) -> str | None:
    """
    A value as it will be read back, months later, by a person.

    JSON is not used: `{"tiers": [{"hoursBefore": 168…` in a table cell is
    unreadable, and the point of this table is that somebody can read it. Lists
    and dicts are rendered compactly and truncated, because a field's *identity*
    plus roughly what it became is what answers the question, and the exact
    former value of a large object is recoverable from the row it replaced.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)[:500] or "(empty)"
    if isinstance(value, dict):
        return ", ".join(f"{k}={v}" for k, v in sorted(value.items()))[:500] or "(empty)"
    return str(value)[:500]


# What "nothing was said here" looks like once stringified.
#
# Needed only on a create. See `diff`.
_EMPTY = frozenset({None, "", "(empty)", "0", "no"})


def diff(before: dict | None, after: dict) -> list[tuple[str, str | None, str | None]]:
    """
    The sensitive fields that actually changed, as (field, old, new).

    Compares *stringified* values rather than raw ones. `10` and `"10"` are the
    same rate to everybody except Python, and a form that round-trips an integer
    through a text input would otherwise produce an audit row every time somebody
    opened it and pressed Save without changing anything — which is how a log
    becomes noise and then becomes ignored.

    A missing key in `after` is not a change to null. PATCH semantics in this
    codebase are "absent means unchanged", and reading absence as deletion here
    would fabricate a history of fields nobody touched.

    **On a create, defaults are not changes.** Found by reading the screen this
    feeds: setting a floor price for the first time wrote seven rows, of which
    four were `enabled: yes`, `days_of_week: (empty)`, `min_nights: 1` and
    `min_units: 1` — every defaulted column on a freshly inserted row, none of
    them chosen by anybody. A history where the four rows nobody cares about
    outnumber the three that matter is a history people stop opening. So when
    there is no prior row, an empty or zero-like value is recorded as silence
    rather than as a decision. An *update* still records everything, including a
    field being cleared, because there the change is real.
    """
    creating = not before
    before = before or {}
    changes = []
    for field, new_value in after.items():
        if field not in SENSITIVE_FIELDS:
            continue
        old = _stringify(before.get(field))
        new = _stringify(new_value)
        if old == new:
            continue
        if creating and new in _EMPTY:
            continue
        changes.append((field, old, new))
    return changes


def _write(rows: list[dict]) -> None:
    get_db().table("hotel_config_audit").insert(rows).execute()


async def record(
    *,
    property_id: str,
    partner_id: str,
    entity: str,
    entity_id: str | None,
    entity_label: str | None,
    before: dict | None,
    after: dict,
    actor_member: str | None = None,
    actor_agent_action: str | None = None,
    actor_label: str = "member",
) -> int:
    """
    Record what changed. Returns how many fields were logged.

    Returns 0 and writes nothing when nothing sensitive moved, which is the
    common case: most saves are a description or a photo, and this table should
    not grow a row for them.
    """
    changes = diff(before, after)
    if not changes:
        return 0

    rows = [
        {
            "property_id": property_id,
            "partner_id": partner_id,
            "entity": entity,
            "entity_id": entity_id,
            "entity_label": entity_label,
            "field": field,
            "old_value": old,
            "new_value": new,
            "actor_member": actor_member,
            "actor_agent_action": actor_agent_action,
            "actor_label": actor_label,
        }
        for field, old, new in changes
    ]

    try:
        await run_db(_write, rows)
    except Exception:  # noqa: BLE001 — see the module docstring
        logger.warning(
            "could not record %d config change(s) on %s %s for property %s",
            len(rows), entity, entity_id, property_id, exc_info=True,
        )
        return 0
    return len(rows)
