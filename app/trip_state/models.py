"""
TripState — the canonical, versioned state object every agent reads and patches.

Design notes:
- Core request/meta fields are strictly typed; deep provider-shaped blobs
  (going/return journeys, hotel items, itinerary days) stay as dicts because
  their shape is owned by the provider normalizers and the V2 data contract
  will tighten them section by section.
- `trip_state_from_merged()` lifts today's supervisor output into a TripState,
  so the monolithic pipeline and the patch-based pipeline share one shape.
- The UI renders TripState sections directly; SSE patches target these paths.
"""
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1


class TripMeta(BaseModel):
    trip_id: Optional[str] = None
    title: Optional[str] = None
    status: str = "planning"          # planning | ready | error
    visibility: str = "private"       # private | link | public
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    # Generation Manifest — what the last successful generation used (models,
    # provider parser versions, spec version, timestamps). Immutable per run.
    generation: Optional[dict] = None
    # Integrity seals, restamped by the store on every committed snapshot —
    # never set by agents. See app.trip_state.integrity.
    checksums: Optional[dict] = None


class TripRequest(BaseModel):
    origin: Optional[str] = None
    destination: Optional[str] = None
    departure_date: Optional[str] = None
    return_date: Optional[str] = None
    days: int = 3
    stay_nights: int = 2
    travelers: int = 1
    budget_tier: str = "mid"              # low | mid | high (presets)
    budget_amount: Optional[int] = None   # real ₹ ceiling, overrides tier when set
    interests: list[str] = Field(default_factory=list)


class TripDestination(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    cover_photo_ref: Optional[str] = None
    currency: Optional[str] = None
    currency_info: dict = Field(default_factory=dict)


class TripTransport(BaseModel):
    going: dict = Field(default_factory=dict)
    return_journey: dict = Field(default_factory=dict)
    # Stopover legs added mid-plan: `going` becomes origin→firstStop and each
    # onward segment carries the next hop (…→destination). First-class so the
    # UI renders the WHOLE outbound route, not just the first leg. `outbound_chain`
    # is the ordered city breadcrumb [origin, …stops…, destination] for a compact
    # "trip goes A → B → C" summary.
    onward_segments: list[dict] = Field(default_factory=list)
    outbound_chain: list[str] = Field(default_factory=list)
    waypoints: list[str] = Field(default_factory=list)
    journey_summary: Optional[str] = None
    total_distance_km: Any = None
    total_duration_approx: Optional[str] = None
    best_time_to_depart: Optional[str] = None


class TripStay(BaseModel):
    hotels: list[dict] = Field(default_factory=list)
    selected: Optional[dict] = None


class TripItinerary(BaseModel):
    days: list[dict] = Field(default_factory=list)
    intelligence: dict = Field(default_factory=dict)


class TripDocuments(BaseModel):
    is_international: bool = False
    visa_required: bool = False
    visa_info: Any = None
    documents_needed: list = Field(default_factory=list)
    health_requirements: list = Field(default_factory=list)
    packing_tips: list = Field(default_factory=list)


class AgentMeta(BaseModel):
    """Reasoning + error metadata; the UI uses this for 'why' surfaces."""
    errors: dict = Field(default_factory=dict)          # section → error message
    validation: dict = Field(default_factory=dict)      # constraint-solver output
    intervention: Any = None                            # supervisor budget action
    notes: list[dict] = Field(default_factory=list)     # [{agent, message, ts}]


class TripState(BaseModel):
    schema_version: int = SCHEMA_VERSION
    meta: TripMeta = Field(default_factory=TripMeta)
    # The immutable Trip Specification — what the user ASKED FOR, versioned.
    # Structured edits append revisions (spec_history) and bump spec.version;
    # every regeneration reads THIS, never the graph. `request` below remains
    # the runtime working copy (return_date resync etc.); spec is the record.
    spec: dict = Field(default_factory=dict)
    request: TripRequest = Field(default_factory=TripRequest)
    destination: TripDestination = Field(default_factory=TripDestination)
    transport: TripTransport = Field(default_factory=TripTransport)
    stay: TripStay = Field(default_factory=TripStay)
    itinerary: TripItinerary = Field(default_factory=TripItinerary)
    costs: dict = Field(default_factory=dict)           # canonical costs block
    documents: TripDocuments = Field(default_factory=TripDocuments)
    weather: dict = Field(default_factory=dict)
    agent_meta: AgentMeta = Field(default_factory=AgentMeta)
    # Canonical multi-city trip graph (nodes=stops, edges=legs). Empty for trips
    # that predate it — lazily derived from the legacy transport/itinerary fields
    # on first builder edit (see app.trip_state.graph). The transport/itinerary
    # blocks above are compatibility projections of this graph. See
    # docs/trip_graph.md.
    graph: dict = Field(default_factory=dict)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def empty_trip_state(request: dict | None = None) -> TripState:
    """Blank state a generation run patches into existence."""
    state = TripState()
    state.meta.created_at = _now()
    state.meta.updated_at = _now()
    if request:
        state.request = TripRequest(**request)
        state.spec = {
            **{k: v for k, v in request.items() if v is not None},
            "stops": request.get("stops") or [],
            "version": 1,
            "created_at": state.meta.created_at,
        }
    return state


def trip_state_from_merged(merged: dict, request: dict | None = None) -> TripState:
    """
    Lift the supervisor's merged plan dict into a TripState.

    Hotels live inside going.hotels in the legacy shape — they're duplicated
    into `stay` (the owned section) and left in transport.going for backward
    compatibility until the app migrates.
    """
    going = merged.get("going") or {}
    state = TripState(
        meta=TripMeta(
            trip_id=str(merged.get("trip_id")) if merged.get("trip_id") else None,
            title=merged.get("trip_name") or merged.get("destination"),
            status="ready",
            created_at=_now(),
            updated_at=_now(),
        ),
        request=TripRequest(
            origin=merged.get("origin"),
            destination=merged.get("destination"),
            departure_date=merged.get("departure_date"),
            return_date=merged.get("return_date"),
            days=merged.get("num_days") or 3,
            stay_nights=merged.get("stay_nights") or 1,
            travelers=merged.get("travelers") or 1,
            budget_tier=merged.get("budget") or "mid",
            interests=(request or {}).get("interests") or [],
        ),
        destination=TripDestination(
            name=merged.get("destination"),
            description=merged.get("destination_description"),
            cover_photo_ref=merged.get("cover_photo_ref"),
            currency=merged.get("currency"),
            currency_info=merged.get("currency_info") or {},
        ),
        transport=TripTransport(
            going=going,
            return_journey=merged.get("return") or {},
            journey_summary=merged.get("journey_summary"),
            total_distance_km=merged.get("total_distance_km"),
            total_duration_approx=merged.get("total_duration_approx"),
            best_time_to_depart=merged.get("best_time_to_depart"),
        ),
        stay=TripStay(
            hotels=(going.get("hotels") or {}).get("items", [])
            if isinstance(going.get("hotels"), dict) else (going.get("hotels") or []),
            selected=(going.get("hotels") or {}).get("recommended")
            if isinstance(going.get("hotels"), dict) else None,
        ),
        itinerary=TripItinerary(
            days=merged.get("days") or [],
            intelligence=merged.get("trip_intelligence") or {},
        ),
        costs=merged.get("costs") or {},
        documents=TripDocuments(
            is_international=bool(merged.get("is_international", False)),
            visa_required=bool(merged.get("visa_required", False)),
            visa_info=merged.get("visa_info"),
            documents_needed=merged.get("documents_needed") or [],
            health_requirements=merged.get("health_requirements") or [],
            packing_tips=merged.get("packing_tips") or [],
        ),
        agent_meta=AgentMeta(
            errors={
                k.replace("_error", ""): v
                for k, v in merged.items() if k.endswith("_error")
            },
            validation=merged.get("trip_validation") or {},
            intervention=(merged.get("costs") or {}).get("intervention"),
        ),
    )
    return state
