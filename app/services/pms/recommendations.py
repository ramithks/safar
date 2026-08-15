"""
Revenue recommendations — a suggestion with its evidence attached, and nothing
that acts on it.

    signal (deterministic)  ──►  recommendation (row)  ──►  a human reads it
         analytics.signals_from        this module            /owner

**A recommendation is a record, not an instruction.** Nothing in this system
executes one. There is no `apply`, no scheduled job that reads these rows, and
no tool that turns one into a rate change — the Owner Copilot holds `reports`
and nothing else, so it could not act on its own advice if it wanted to. The
eventual path is recommendation → approval → revalidation → execution, and the
first three of those already exist for other tools; this is deliberately only
the first.

**Evidence is server-supplied and validated on write.** `create()` refuses a
recommendation whose evidence does not reference metrics the server actually
produced for that period. That is the difference between

    "Increase room 102 pricing by 20% because demand is high"

and

    "Occupancy for the next 7 days is 91% against 74% in the comparable
     period; 6 of 8 rooms in this type are sold. Review the public rate."

The second is checkable. The first contains a precision nobody computed, and a
model will produce it every time unless the write path refuses it — a system
prompt asking nicely is not a control.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from app.db.client import get_db, run_db
from app.services.ai.errors import OutOfScope

logger = logging.getLogger(__name__)

# The eight the product starts with. Deliberately closed: a recommendation type
# nobody has designed a card for is one that renders as a blank box, and a free
# text `type` column is how a taxonomy becomes forty near-synonyms.
TYPES = (
    "pricing_opportunity",
    "low_occupancy",
    "high_demand",
    "room_type_underperformance",
    "cancellation_spike",
    "booking_pace_change",
    "channel_performance",
    "revenue_anomaly",
)

CONFIDENCE = ("low", "medium", "high")
STATUSES = ("open", "reviewed", "dismissed", "actioned")

# Metric keys a piece of evidence may cite. The same keys `analytics.metrics_from`
# emits, so a citation either names a number the server computed or is refused.
CITABLE = frozenset({
    "occupancy", "adr", "revpar", "room_revenue", "service_revenue",
    "total_revenue", "rooms_sold", "capacity", "booking_pace", "bookings_made",
    "cancellation_rate", "cancellations", "alos", "arrivals", "departures",
})


class EvidenceRejected(OutOfScope):
    """
    The recommendation cited something the server did not produce.

    An `AgentError`, so `registry.execute` turns it into a refusal the model is
    shown and can correct — rather than an exception that ends the turn. A model
    told "occupancy is not among the metrics for this period" writes a better
    recommendation on the next call; a model told nothing writes the same one.
    """


@dataclass(frozen=True)
class Evidence:
    metric: str
    observed: float | int | None
    comparison: float | int | None
    unit: str
    note: str

    def as_dict(self) -> dict:
        return {
            "metric": self.metric,
            "observed": self.observed,
            "comparison": self.comparison,
            "unit": self.unit,
            "note": self.note,
        }


def check_evidence(items: list[dict], server_metrics: list[dict]) -> list[dict]:
    """
    Every citation must name a metric the server computed, and carry its value.

    Returns the evidence stamped with what the server actually holds, so the
    stored row carries the *server's* number rather than the model's copy of it.
    A model that rounds 72.4 to "about 75" therefore cannot put 75 into the
    record, which is the only version of this check that is worth having.
    """
    if not items:
        raise EvidenceRejected(
            "a recommendation must cite at least one metric from this period",
            "I need to look at the numbers again before suggesting that.",
        )

    by_key = {m["key"]: m for m in server_metrics}
    out: list[dict] = []
    for item in items[:8]:
        key = str(item.get("metric") or "").strip()
        if key not in CITABLE:
            raise EvidenceRejected(
                f"{key!r} is not a metric this system produces; cite one of "
                f"{', '.join(sorted(CITABLE))}",
            )
        served = by_key.get(key)
        if served is None:
            raise EvidenceRejected(
                f"{key} was not computed for this period, so it cannot be cited",
            )
        if served.get("value") is None:
            raise EvidenceRejected(
                f"{key} has no value for this period — say so rather than "
                f"recommending from it",
            )
        if not served.get("sufficient", True):
            raise EvidenceRejected(
                f"{key} rests on {served.get('sample')} observations, which is "
                f"below this property's threshold. Return INSUFFICIENT_DATA "
                f"instead of a recommendation.",
            )
        out.append({
            "metric": key,
            "label": served.get("label"),
            # The server's numbers, not the model's. This is the line.
            "observed": served.get("value"),
            "comparison": served.get("previous"),
            "change": served.get("change"),
            "changePct": served.get("changePct"),
            "unit": served.get("unit"),
            "currency": served.get("currency"),
            "basis": served.get("basis"),
            "sample": served.get("sample"),
            "note": str(item.get("note") or "")[:300],
        })
    return out


def _insert(row: dict) -> dict:
    return get_db().table("revenue_recommendations").insert(row).execute().data[0]


async def create(
    *,
    property_id: str,
    partner_id: str,
    rec_type: str,
    title: str,
    rationale: str,
    evidence: list[dict],
    server_metrics: list[dict],
    period: dict,
    confidence: str = "medium",
    expected_impact: str | None = None,
    risk: str | None = None,
    suggested_action: str | None = None,
    subject: dict | None = None,
    agent_id: str | None = None,
    run_id: str | None = None,
) -> dict:
    """
    Write one recommendation. Refuses rather than downgrades.

    `server_metrics` is the metric list the tool itself just read from
    `analytics`. Passing it in — rather than letting this module re-read — is
    what makes the check meaningful: the evidence is validated against the very
    numbers the model was shown, not against a fresh read that may have moved.
    """
    if rec_type not in TYPES:
        raise EvidenceRejected(
            f"{rec_type!r} is not a recommendation type; use one of {', '.join(TYPES)}"
        )
    if confidence not in CONFIDENCE:
        confidence = "medium"

    checked = check_evidence(evidence, server_metrics)

    row = {
        "property_id": property_id,
        "partner_id": partner_id,
        "type": rec_type,
        "status": "open",
        "title": title[:200],
        "rationale": rationale[:2000],
        "confidence": confidence,
        "expected_impact": (expected_impact or "")[:500] or None,
        "risk": (risk or "")[:500] or None,
        "suggested_action": (suggested_action or "")[:500] or None,
        "evidence": checked,
        "period": period,
        "subject": subject or {},
        "agent_id": agent_id,
        "run_id": run_id,
        "created_by": "agent" if agent_id else "system",
    }
    return await run_db(_insert, row)


async def list_for(property_id: str, *, status: str | None = None,
                   limit: int = 40) -> list[dict]:
    def _read():
        q = (
            get_db().table("revenue_recommendations").select("*")
            .eq("property_id", property_id)
        )
        if status:
            q = q.eq("status", status)
        return q.order("created_at", desc=True).limit(limit).execute().data or []

    return await run_db(_read)


async def set_status(property_id: str, recommendation_id: str, status: str,
                     *, profile_id: str | None = None) -> dict | None:
    """
    Mark one reviewed or dismissed.

    `actioned` is in the enumeration and is set by a *human* saying they did it,
    never by this system doing it. When an execution path exists it will go
    through approval and revalidation like every other write, and it will set
    this column as a side effect of that — not instead of it.
    """
    if status not in STATUSES:
        raise ValueError(f"unknown recommendation status {status!r}")

    def _write():
        rows = (
            get_db().table("revenue_recommendations")
            .update({
                "status": status,
                "reviewed_by": profile_id,
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
            })
            .eq("id", recommendation_id).eq("property_id", property_id)
            .execute().data
        )
        return rows[0] if rows else None

    return await run_db(_write)


def project(row: dict) -> dict:
    """The API shape. One projection, so every surface shows the same fields."""
    return {
        "id": row["id"],
        "propertyId": row["property_id"],
        "type": row["type"],
        "status": row["status"],
        "title": row["title"],
        "rationale": row["rationale"],
        "confidence": row["confidence"],
        "expectedImpact": row.get("expected_impact"),
        "risk": row.get("risk"),
        "suggestedAction": row.get("suggested_action"),
        "evidence": row.get("evidence") or [],
        "period": row.get("period") or {},
        "subject": row.get("subject") or {},
        "createdBy": row.get("created_by"),
        "createdAt": row.get("created_at"),
        "reviewedAt": row.get("reviewed_at"),
        "agentActionId": row.get("agent_action_id"),
    }
