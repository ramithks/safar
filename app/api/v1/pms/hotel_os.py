"""
The hotel's own configuration: rooms in full, rules, team, readiness.

A second router on the same `/pms` prefix rather than more of `endpoint.py`,
which is already 5,500 lines. Nothing about the mounting differs — it goes in
beside the first one, behind the same `ENABLE_HOTEL_OS` flag, with the same
limiter and the same `require_member` dependency.

**Three rules hold across every route here.**

*The property comes from the path and is checked by the dependency.* An id in a
body is scoped to it on update, so a row belonging to another hotel updates
nothing rather than being moved. That is the same pattern `upsert_service`
established and it is what makes tenant isolation a property of the route rather
than of the caller's good behaviour.

*Writing a price is `rates`.* Reading one is not. A receptionist quoting a
transfer needs its price and may not decide it, which is the line the rate
calendar already draws. Team routes are `members`, escalations are `messaging`.

*Sensitive changes are recorded.* Every write that can move money or access goes
through `audit.record`, which writes one row per changed field and never fails
the change it is recording.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.v1.pms.hotel_os_schemas import (
    AdCampaignOut,
    AdCampaignRequest,
    AdCreativeOut,
    AdCreativeRequest,
    AdDraftOut,
    AdPublishOut,
    AudienceOut,
    AudienceRequest,
    ContextHealthOut,
    EscalationOut,
    EscalationResolveRequest,
    ExperienceOut,
    ExperienceRequest,
    GroupPolicyOut,
    GroupPolicyRequest,
    GroupTierOut,
    GroupTierRequest,
    MarketingProfileOut,
    MarketingProfileRequest,
    MealOptionOut,
    MealOptionRequest,
    NegotiationPreviewOut,
    NegotiationPreviewRequest,
    NegotiationRuleOut,
    NegotiationRuleRequest,
    NotificationRuleOut,
    NotificationRuleRequest,
    RatePlanDetailOut,
    RatePlanDetailRequest,
    ReadinessReportOut,
    RoomTypeDetailOut,
    RoomTypeDetailRequest,
    StayTimeRuleOut,
    StayTimeRuleRequest,
    TeamMemberOut,
    TeamMemberRequest,
)
from app.core.limiter import limiter
from app.db.client import get_db, run_db
from app.deps.property import require_member
from app.services.marketing import creatives, meta_ads
from app.services.pms import audit as audit_service
from app.services.pms import negotiation as negotiation_service
from app.services.pms import readiness as readiness_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/pms", tags=["pms"])


# ── shared helpers ───────────────────────────────────────────────────────────


def _scoped(table: str, property_id: str):
    return get_db().table(table).select("*").eq("property_id", property_id)


def _upsert(
    table: str, property_id: str, partner_id: str, row_id: str | None, columns: dict,
) -> dict | None:
    """
    Create or amend one row of a property-scoped table.

    **The update is scoped by property as well as by id**, which is the line that
    matters: without `.eq("property_id", …)` a caller who guessed a uuid from
    another hotel would move that hotel's row into theirs. With it, the same
    request updates nothing and answers 404.
    """
    db = get_db()
    if row_id:
        rows = (
            db.table(table).update({**columns, "updated_at": "now()"})
            .eq("id", row_id).eq("property_id", property_id).execute().data
        )
        return rows[0] if rows else None
    return db.table(table).insert(
        {**columns, "property_id": property_id, "partner_id": partner_id}
    ).execute().data[0]


def _delete(table: str, property_id: str, row_id: str) -> bool:
    rows = (
        get_db().table(table).delete()
        .eq("id", row_id).eq("property_id", property_id).execute().data
    )
    return bool(rows)


def _read_one(table: str, property_id: str, row_id: str) -> dict | None:
    rows = (
        get_db().table(table).select("*")
        .eq("id", row_id).eq("property_id", property_id).limit(1).execute().data or []
    )
    return rows[0] if rows else None


# ── rooms, in full ───────────────────────────────────────────────────────────


@router.get("/properties/{property_id}/room-types-detail",
            response_model=list[RoomTypeDetailOut])
@limiter.limit("120/minute")
async def list_room_type_details(
    request: Request,
    property_id: str,
    include_inactive: bool = Query(False),
    member=Depends(require_member()),
):
    """
    Every room type with every field, and whether each may be sold.

    Separate from the existing `/room-types`, which returns the shape the
    calendar and the booking drawer read and has done since before most of these
    columns existed. Widening that projection would have made every screen in the
    dashboard pay for a room editor only one of them opens.

    **The path is `room-types-detail` and not `room-types/detail`**, which is
    what it was until the e2e ran. `PATCH /room-types/{room_type_id}` already
    exists on the other router and is registered first, so `detail` matched as a
    room type id and the GET answered 405 Method Not Allowed — a routing
    collision that looks exactly like a broken endpoint. A literal segment that
    can be mistaken for a path parameter is a trap whichever way it resolves.
    """
    def _read():
        query = (
            get_db().table("room_types").select("*")
            .eq("property_id", property_id).order("sort_order")
        )
        if not include_inactive:
            query = query.eq("is_active", True)
        rooms = query.limit(200).execute().data or []
        plans = (
            get_db().table("rate_plans").select("*")
            .eq("property_id", property_id).eq("is_active", True)
            .limit(400).execute().data or []
        )
        return rooms, plans

    rooms, plans = await run_db(_read)
    by_room: dict[str, list[dict]] = {}
    for plan in plans:
        by_room.setdefault(str(plan.get("room_type_id")), []).append(plan)

    return [
        RoomTypeDetailOut.from_row(
            room,
            readiness_service.room_result(room, by_room.get(str(room["id"]), []), {}),
        )
        for room in rooms
    ]


@router.patch("/properties/{property_id}/room-types/{room_type_id}/detail",
              response_model=RoomTypeDetailOut)
@limiter.limit("120/minute")
async def update_room_type_detail(
    request: Request,
    property_id: str,
    room_type_id: str,
    body: RoomTypeDetailRequest,
    member=Depends(require_member("rates")),
):
    """
    Save part or all of a room.

    Absent means unchanged; an empty value clears. That contract is what lets a
    ten-step wizard save each step as it is finished — which is the difference
    between a form people abandon halfway and one they come back to.

    **Photo metadata merges, the photo list replaces.** A hotelier reordering
    their photos sends the whole array, because order is the array; a hotelier
    captioning one photo sends one key, because captioning the second should not
    erase the first.
    """
    columns = body.to_columns()
    if not columns:
        existing = await run_db(_read_one, "room_types", property_id, room_type_id)
        if not existing:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Room not found")
        return RoomTypeDetailOut.from_row(existing)

    def _write():
        db = get_db()
        before = _read_one("room_types", property_id, room_type_id)
        if not before:
            return None, None

        patch = dict(columns)
        if "photo_meta" in patch:
            merged = dict(before.get("photo_meta") or {})
            for url, meta in (patch["photo_meta"] or {}).items():
                cleaned = {k: v for k, v in (meta or {}).items() if k in {"caption", "alt"}}
                if cleaned:
                    merged[url] = cleaned
                else:
                    merged.pop(url, None)
            # Metadata for a photo that is no longer on the room is dropped, so
            # deleting and re-adding a picture does not resurrect a caption
            # somebody wrote about a different one.
            keep = set(patch.get("photos", before.get("photos") or []))
            patch["photo_meta"] = {k: v for k, v in merged.items() if k in keep}

        rows = (
            db.table("room_types").update({**patch, "updated_at": "now()"})
            .eq("id", room_type_id).eq("property_id", property_id).execute().data
        )
        if not rows:
            return None, None

        plans = (
            db.table("rate_plans").select("*")
            .eq("property_id", property_id).eq("room_type_id", room_type_id)
            .eq("is_active", True).limit(50).execute().data or []
        )
        return rows[0], (before, plans)

    updated, context = await run_db(_write)
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Room not found")
    before, plans = context

    await audit_service.record(
        property_id=property_id, partner_id=member.partner_id,
        entity="room_type", entity_id=room_type_id, entity_label=updated.get("name"),
        before=before, after=columns, actor_member=member.member_id,
    )
    return RoomTypeDetailOut.from_row(
        updated, readiness_service.room_result(updated, plans, {})
    )


# ── arriving early, leaving late ─────────────────────────────────────────────


@router.get("/properties/{property_id}/stay-time-rules", response_model=list[StayTimeRuleOut])
@limiter.limit("120/minute")
async def list_stay_time_rules(
    request: Request, property_id: str, member=Depends(require_member()),
):
    def _read():
        return (
            _scoped("stay_time_rules", property_id)
            .order("kind").order("until_time").limit(50).execute().data or []
        )

    return [StayTimeRuleOut.from_row(r) for r in await run_db(_read)]


@router.post("/properties/{property_id}/stay-time-rules", response_model=StayTimeRuleOut)
@limiter.limit("60/minute")
async def upsert_stay_time_rule(
    request: Request,
    property_id: str,
    body: StayTimeRuleRequest,
    member=Depends(require_member("rates")),
):
    """
    One band of the early-arrival or late-departure ladder.

    `rates`, because this sets a charge. A duplicate time is refused by the
    database's own unique constraint rather than by a check here — two rows for
    10:00 would make the answer depend on insertion order, and a policy may not
    do that.
    """
    columns = body.to_columns()

    def _write():
        before = _read_one("stay_time_rules", property_id, body.id) if body.id else None
        try:
            return _upsert("stay_time_rules", property_id, member.partner_id, body.id, columns), before
        except Exception as exc:  # noqa: BLE001
            if "stay_time_rules_property_id_kind_until_time_key" in str(exc):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "You already have a rule ending at that time. Edit it instead.",
                ) from exc
            raise

    row, before = await run_db(_write)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")
    await audit_service.record(
        property_id=property_id, partner_id=member.partner_id,
        entity="stay_time_rule", entity_id=str(row["id"]),
        entity_label=f"{body.kind} until {body.untilTime}",
        before=before, after=columns, actor_member=member.member_id,
    )
    return StayTimeRuleOut.from_row(row)


@router.delete("/properties/{property_id}/stay-time-rules/{rule_id}", status_code=204)
@limiter.limit("60/minute")
async def delete_stay_time_rule(
    request: Request, property_id: str, rule_id: str,
    member=Depends(require_member("rates")),
):
    if not await run_db(_delete, "stay_time_rules", property_id, rule_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")


# ── meals ────────────────────────────────────────────────────────────────────


@router.get("/properties/{property_id}/meals", response_model=list[MealOptionOut])
@limiter.limit("120/minute")
async def list_meals(request: Request, property_id: str, member=Depends(require_member())):
    def _read():
        return _scoped("meal_options", property_id).limit(10).execute().data or []

    return [MealOptionOut.from_row(r) for r in await run_db(_read)]


@router.put("/properties/{property_id}/meals", response_model=MealOptionOut)
@limiter.limit("60/minute")
async def upsert_meal(
    request: Request,
    property_id: str,
    body: MealOptionRequest,
    member=Depends(require_member("rates")),
):
    """
    Describe one meal.

    Keyed on `(property, meal)` rather than on an id, because there is exactly
    one breakfast and a hotel editing it is amending a statement rather than
    adding a second one. `on_conflict` does the work, so a screen that saves
    twice produces one row.
    """
    columns = body.to_columns()

    def _write():
        db = get_db()
        before_rows = (
            db.table("meal_options").select("*")
            .eq("property_id", property_id).eq("meal", body.meal).limit(1).execute().data or []
        )
        rows = db.table("meal_options").upsert(
            {**columns, "property_id": property_id, "partner_id": member.partner_id,
             "updated_at": "now()"},
            on_conflict="property_id,meal",
        ).execute().data
        return rows[0], (before_rows[0] if before_rows else None)

    row, before = await run_db(_write)
    await audit_service.record(
        property_id=property_id, partner_id=member.partner_id,
        entity="meal", entity_id=str(row["id"]), entity_label=body.meal,
        before=before, after=columns, actor_member=member.member_id,
    )
    return MealOptionOut.from_row(row)


# ── negotiation ──────────────────────────────────────────────────────────────


@router.get("/properties/{property_id}/negotiation-rules",
            response_model=list[NegotiationRuleOut])
@limiter.limit("60/minute")
async def list_negotiation_rules(
    request: Request, property_id: str, member=Depends(require_member("rates")),
):
    """
    A hotel's own floors, read back to the hotel.

    **`require_member("rates")` and not the usual open read.** This is the one
    projection in the product that carries a floor price, and the floor is the
    number whose leak costs the hotel money on every future negotiation. A
    receptionist who knows it will eventually say it. The database agrees — the
    table's own read policy is `has_partner_perm(partner_id, 'rates')` — so this
    is the same decision stated in both places rather than a check the API could
    forget.
    """
    def _read():
        return _scoped("negotiation_rules", property_id).limit(100).execute().data or []

    return [NegotiationRuleOut.from_row(r) for r in await run_db(_read)]


@router.post("/properties/{property_id}/negotiation-rules", response_model=NegotiationRuleOut)
@limiter.limit("60/minute")
async def upsert_negotiation_rule(
    request: Request,
    property_id: str,
    body: NegotiationRuleRequest,
    member=Depends(require_member("rates")),
):
    """
    Set or amend the rules for one hotel, room type or rate plan.

    Upserted on the scope rather than on an id: a hotel has one rule per scoped
    thing, and a screen that saved a second would produce a precedence question
    with no answer. The partial unique indexes enforce it; this route uses them
    rather than checking first, because checking first is a race.

    **The room type or rate plan named must belong to this property.** Checked
    here rather than left to the foreign key, which only knows the row exists
    somewhere — a rule scoped to another hotel's rate plan would be invisible to
    both hotels and would quietly never fire.
    """
    columns = body.to_columns()

    def _write():
        db = get_db()

        for column, table in (("room_type_id", "room_types"), ("rate_plan_id", "rate_plans")):
            target = columns.get(column)
            if target and not _read_one(table, property_id, target):
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    "That room or rate plan is not part of this hotel.",
                )

        # What this rule is *about*, for the history screen. `body.scope` was
        # used here first, which rendered as "room_type" in a log a hotelier is
        # meant to read — the whole point of that table is that somebody can
        # answer "when did the Pool Villa's floor drop" without a schema.
        label = "The whole hotel"
        for column, table, name_column in (
            ("room_type_id", "room_types", "name"), ("rate_plan_id", "rate_plans", "name"),
        ):
            target = columns.get(column)
            if target:
                row = _read_one(table, property_id, target)
                label = (row or {}).get(name_column) or label

        query = db.table("negotiation_rules").select("*").eq("property_id", property_id).eq(
            "scope", body.scope
        )
        if body.scope == "room_type":
            query = query.eq("room_type_id", columns["room_type_id"])
        elif body.scope == "rate_plan":
            query = query.eq("rate_plan_id", columns["rate_plan_id"])
        existing = query.limit(1).execute().data or []

        if existing:
            rows = (
                db.table("negotiation_rules")
                .update({**columns, "updated_at": "now()"})
                .eq("id", existing[0]["id"]).eq("property_id", property_id)
                .execute().data
            )
            return rows[0], existing[0], label
        return db.table("negotiation_rules").insert(
            {**columns, "property_id": property_id, "partner_id": member.partner_id}
        ).execute().data[0], None, label

    row, before, label = await run_db(_write)
    await audit_service.record(
        property_id=property_id, partner_id=member.partner_id,
        entity="negotiation_rule", entity_id=str(row["id"]), entity_label=label,
        before=before, after=columns, actor_member=member.member_id,
    )
    return NegotiationRuleOut.from_row(row)


@router.delete("/properties/{property_id}/negotiation-rules/{rule_id}", status_code=204)
@limiter.limit("60/minute")
async def delete_negotiation_rule(
    request: Request, property_id: str, rule_id: str,
    member=Depends(require_member("rates")),
):
    """
    Remove a rule, falling back to the next scope up.

    Deleting a rate plan's rule does not make that plan non-negotiable; it makes
    it follow the room type's rule, or the hotel's, or the hotel's settings. That
    is what "most specific wins" means from the other direction, and it is worth
    saying out loud because the opposite reading — delete means off — is the one
    a hotelier might expect.
    """
    before = await run_db(_read_one, "negotiation_rules", property_id, rule_id)
    if not before or not await run_db(_delete, "negotiation_rules", property_id, rule_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")
    await audit_service.record(
        property_id=property_id, partner_id=member.partner_id,
        entity="negotiation_rule", entity_id=rule_id,
        entity_label=f"{before.get('scope')} (deleted)",
        before=before, after={k: None for k in audit_service.SENSITIVE_FIELDS if k in before},
        actor_member=member.member_id,
    )


@router.post("/properties/{property_id}/negotiation-preview",
             response_model=NegotiationPreviewOut)
@limiter.limit("60/minute")
async def preview_negotiation(
    request: Request,
    property_id: str,
    body: NegotiationPreviewRequest,
    member=Depends(require_member("rates")),
):
    """
    "What would my AI say if a guest asked for this?"

    The real engine, against the real calendar, with the real rules — not a
    simulation. A hotelier cannot be asked to trust a floor they have no way to
    test, and the only test worth anything is the one that runs the same function
    the guest's message will run at 2am.

    Nothing is written and no stock is held. A preview that took a hold would let
    a hotelier playing with numbers make their own rooms unavailable.
    """
    decision = await negotiation_service.negotiate(
        property_id=property_id,
        rate_plan_id=body.ratePlanId,
        check_in=body.checkIn,
        check_out=body.checkOut,
        units=body.units,
        requested_nightly_paise=(
            round(body.requestedNightlyRupees * 100)
            if body.requestedNightlyRupees is not None else None
        ),
    )
    return NegotiationPreviewOut(**decision.as_dict(), wouldSay=_would_say(decision))


def _would_say(decision) -> str:
    """
    The sentence a hotelier reads on the preview screen.

    **This is not the sentence the guest gets, and must never become it.** The
    agent writes to the guest in their language, in its own voice, from the same
    decision — that is the model's job and the whole reason the engine returns a
    verdict rather than prose. This is English, plain, and exists so a hotelier
    can see what their rules add up to without learning what `needs_approval`
    means.
    """
    rupees = lambda paise: f"₹{paise / 100:,.0f}"  # noqa: E731
    if decision.verdict == "allowed":
        return f"Yes — {rupees(decision.final_nightly_paise)} a night, confirmed on the spot."
    if decision.verdict == "counter":
        line = f"Not at that price, but {rupees(decision.counter_nightly_paise)} a night is fine."
        return line + (" I can also ask the hotel." if decision.approval_available else "")
    if decision.verdict == "needs_approval":
        return "Let me check with the hotel and come back to you."
    return {
        "negotiation_closed": "These rooms are not negotiable.",
        "blackout_dates": "Those dates are blocked out for offers.",
        "outside_valid_dates": "Offers are not running for those dates.",
        "outside_valid_days": "Offers are not running on those days.",
        "below_minimum_nights": "That stay is too short to negotiate on.",
        "below_minimum_rooms": "That is too few rooms to negotiate on.",
        "not_enough_rooms": "There are not enough rooms free for that.",
        "no_rate_available": "There is no rate open for those dates.",
    }.get(decision.reason, "No offer available.")


# ── group pricing ────────────────────────────────────────────────────────────


@router.get("/properties/{property_id}/group-tiers", response_model=list[GroupTierOut])
@limiter.limit("120/minute")
async def list_group_tiers(request: Request, property_id: str, member=Depends(require_member())):
    def _read():
        return _scoped("group_rate_tiers", property_id).order("min_units").limit(50).execute().data or []

    return [GroupTierOut.from_row(r) for r in await run_db(_read)]


@router.post("/properties/{property_id}/group-tiers", response_model=GroupTierOut)
@limiter.limit("60/minute")
async def upsert_group_tier(
    request: Request,
    property_id: str,
    body: GroupTierRequest,
    member=Depends(require_member("rates")),
):
    if body.maxUnits is not None and body.maxUnits < body.minUnits:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "The top of the band cannot be below its bottom.",
        )
    if body.discountPct is None and body.fixedPriceRupees is None and not body.negotiable:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Say what changes at this size: a discount, a fixed rate, or that it is negotiable.",
        )

    columns = body.to_columns()

    def _write():
        if body.roomTypeId and not _read_one("room_types", property_id, body.roomTypeId):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "That room is not part of this hotel.")
        before = _read_one("group_rate_tiers", property_id, body.id) if body.id else None
        return _upsert("group_rate_tiers", property_id, member.partner_id, body.id, columns), before

    row, before = await run_db(_write)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tier not found")
    await audit_service.record(
        property_id=property_id, partner_id=member.partner_id,
        entity="group_tier", entity_id=str(row["id"]),
        entity_label=f"{body.minUnits}+ rooms",
        before=before, after=columns, actor_member=member.member_id,
    )
    return GroupTierOut.from_row(row)


@router.delete("/properties/{property_id}/group-tiers/{tier_id}", status_code=204)
@limiter.limit("60/minute")
async def delete_group_tier(
    request: Request, property_id: str, tier_id: str,
    member=Depends(require_member("rates")),
):
    if not await run_db(_delete, "group_rate_tiers", property_id, tier_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tier not found")


@router.get("/properties/{property_id}/group-policy", response_model=GroupPolicyOut)
@limiter.limit("120/minute")
async def get_group_policy(request: Request, property_id: str, member=Depends(require_member())):
    def _read():
        rows = (
            get_db().table("group_policies").select("*")
            .eq("property_id", property_id).limit(1).execute().data or []
        )
        return rows[0] if rows else None

    return GroupPolicyOut.from_row(await run_db(_read))


@router.put("/properties/{property_id}/group-policy", response_model=GroupPolicyOut)
@limiter.limit("60/minute")
async def set_group_policy(
    request: Request,
    property_id: str,
    body: GroupPolicyRequest,
    member=Depends(require_member("rates")),
):
    columns = body.to_columns()

    def _write():
        db = get_db()
        before_rows = (
            db.table("group_policies").select("*")
            .eq("property_id", property_id).limit(1).execute().data or []
        )
        rows = db.table("group_policies").upsert(
            {**columns, "property_id": property_id, "partner_id": member.partner_id,
             "updated_at": "now()"},
            on_conflict="property_id",
        ).execute().data
        return rows[0], (before_rows[0] if before_rows else None)

    row, before = await run_db(_write)
    await audit_service.record(
        property_id=property_id, partner_id=member.partner_id,
        entity="group_policy", entity_id=None, entity_label="group policy",
        before=before, after=columns, actor_member=member.member_id,
    )
    return GroupPolicyOut.from_row(row)


# ── experiences ──────────────────────────────────────────────────────────────


@router.get("/properties/{property_id}/experiences", response_model=list[ExperienceOut])
@limiter.limit("120/minute")
async def list_experiences(
    request: Request,
    property_id: str,
    include_inactive: bool = Query(False),
    member=Depends(require_member()),
):
    def _read():
        query = _scoped("experiences", property_id).order("sort_order")
        if not include_inactive:
            query = query.eq("is_active", True)
        return query.limit(200).execute().data or []

    return [ExperienceOut.from_row(r) for r in await run_db(_read)]


@router.post("/properties/{property_id}/experiences", response_model=ExperienceOut)
@limiter.limit("60/minute")
async def upsert_experience(
    request: Request,
    property_id: str,
    body: ExperienceRequest,
    member=Depends(require_member("rates")),
):
    """
    Create or amend one experience. `rates`, because it carries a price.
    """
    if body.maxParticipants is not None and body.maxParticipants < body.minParticipants:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "The maximum party size cannot be below the minimum.",
        )
    if body.maxAge is not None and body.minAge is not None and body.maxAge < body.minAge:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "The age range is the wrong way round.",
        )

    columns = body.to_columns()
    row = await run_db(_upsert, "experiences", property_id, member.partner_id, body.id, columns)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Experience not found")
    return ExperienceOut.from_row(row)


@router.delete("/properties/{property_id}/experiences/{experience_id}", status_code=204)
@limiter.limit("60/minute")
async def delete_experience(
    request: Request, property_id: str, experience_id: str,
    member=Depends(require_member("rates")),
):
    if not await run_db(_delete, "experiences", property_id, experience_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Experience not found")


@router.delete("/properties/{property_id}/services/{service_id}", status_code=204)
@limiter.limit("60/minute")
async def delete_service(
    request: Request, property_id: str, service_id: str,
    member=Depends(require_member("rates")),
):
    """
    Retire a service permanently.

    The dashboard should prefer `isActive: false` — a service that has been on a
    folio has history, and deleting the row leaves `reservation_services.name`
    as the only record of what a guest was charged for. Offered because a hotel
    that added "Bonefire" by mistake should not have to live with it forever.
    """
    if not await run_db(_delete, "services", property_id, service_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Service not found")


# ── the team ─────────────────────────────────────────────────────────────────


@router.get("/properties/{property_id}/team", response_model=list[TeamMemberOut])
@limiter.limit("120/minute")
async def list_team(
    request: Request,
    property_id: str,
    include_inactive: bool = Query(False),
    member=Depends(require_member()),
):
    """
    Who to contact and how.

    Readable by anyone who works here: a receptionist needs the manager's number
    at 11pm, and that is the case this table exists for. Editing is `members`.
    """
    def _read():
        query = _scoped("hotel_team_members", property_id).order("sort_order").order("name")
        if not include_inactive:
            query = query.eq("is_active", True)
        return query.limit(200).execute().data or []

    return [TeamMemberOut.from_row(r) for r in await run_db(_read)]


@router.post("/properties/{property_id}/team", response_model=TeamMemberOut)
@limiter.limit("60/minute")
async def upsert_team_member(
    request: Request,
    property_id: str,
    body: TeamMemberRequest,
    member=Depends(require_member("members")),
):
    columns = body.to_columns()
    if not any((columns["whatsapp"], columns["email"], columns["phone"], columns["member_id"])):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Give at least one way to reach them — a WhatsApp number, a phone number or an email.",
        )

    def _write():
        before = _read_one("hotel_team_members", property_id, body.id) if body.id else None
        try:
            return _upsert(
                "hotel_team_members", property_id, member.partner_id, body.id, columns,
            ), before
        except Exception as exc:  # noqa: BLE001
            if "hotel_team_members_whatsapp_uniq" in str(exc):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "Somebody on your team already has that WhatsApp number.",
                ) from exc
            raise

    row, before = await run_db(_write)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Team member not found")
    await audit_service.record(
        property_id=property_id, partner_id=member.partner_id,
        entity="team_member", entity_id=str(row["id"]), entity_label=body.name,
        before=before, after=columns, actor_member=member.member_id,
    )
    return TeamMemberOut.from_row(row)


@router.delete("/properties/{property_id}/team/{team_member_id}", status_code=204)
@limiter.limit("60/minute")
async def delete_team_member(
    request: Request, property_id: str, team_member_id: str,
    member=Depends(require_member("members")),
):
    """
    Remove somebody from the directory.

    Their notification rules go with them — the foreign key cascades — which is
    the right behaviour and worth naming: a rule pointing at a deleted person is
    an escalation that silently reaches nobody, which is the exact failure this
    whole path exists to prevent.
    """
    if not await run_db(_delete, "hotel_team_members", property_id, team_member_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Team member not found")


@router.get("/properties/{property_id}/notification-rules",
            response_model=list[NotificationRuleOut])
@limiter.limit("120/minute")
async def list_notification_rules(
    request: Request, property_id: str, member=Depends(require_member()),
):
    def _read():
        rules = _scoped("team_notification_rules", property_id).order("event").limit(300).execute().data or []
        if not rules:
            return [], {}
        people = (
            get_db().table("hotel_team_members").select("id,name")
            .eq("property_id", property_id).limit(200).execute().data or []
        )
        return rules, {str(p["id"]): p.get("name") for p in people}

    rules, names = await run_db(_read)
    return [NotificationRuleOut.from_row(r, names.get(str(r["team_member_id"]))) for r in rules]


@router.post("/properties/{property_id}/notification-rules", response_model=NotificationRuleOut)
@limiter.limit("60/minute")
async def upsert_notification_rule(
    request: Request,
    property_id: str,
    body: NotificationRuleRequest,
    member=Depends(require_member("members")),
):
    """
    Who hears about what.

    Upserted on `(property, event, member)` because that triple *is* the rule —
    a second row for the same person and the same event would send two messages
    to one phone.
    """
    def _write():
        person = _read_one("hotel_team_members", property_id, body.teamMemberId)
        if not person:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "That person is not on your team.")
        rows = get_db().table("team_notification_rules").upsert(
            {
                "property_id": property_id,
                "partner_id": member.partner_id,
                "event": body.event,
                "team_member_id": body.teamMemberId,
                "channels": body.channels,
                "is_active": body.isActive,
                "updated_at": "now()",
            },
            on_conflict="property_id,event,team_member_id",
        ).execute().data
        return rows[0], person.get("name")

    row, name = await run_db(_write)
    return NotificationRuleOut.from_row(row, name)


@router.delete("/properties/{property_id}/notification-rules/{rule_id}", status_code=204)
@limiter.limit("60/minute")
async def delete_notification_rule(
    request: Request, property_id: str, rule_id: str,
    member=Depends(require_member("members")),
):
    if not await run_db(_delete, "team_notification_rules", property_id, rule_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")


# ── escalations ──────────────────────────────────────────────────────────────


@router.get("/properties/{property_id}/escalations", response_model=list[EscalationOut])
@limiter.limit("120/minute")
async def list_escalations(
    request: Request,
    property_id: str,
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    member=Depends(require_member()),
):
    """
    What the AI has asked the hotel about, and whether anybody answered.

    Ordered newest first and defaulted to everything rather than to open, because
    the question a manager opens this screen with is usually "what happened
    yesterday" rather than "what is outstanding" — and the outstanding ones are
    at the top of that list anyway.
    """
    def _read():
        query = _scoped("agent_escalations", property_id).order("created_at", desc=True)
        if status_filter:
            query = query.eq("status", status_filter)
        rows = query.limit(limit).execute().data or []
        if not rows:
            return [], {}
        people = (
            get_db().table("hotel_team_members").select("id,name")
            .eq("property_id", property_id).limit(200).execute().data or []
        )
        return rows, {str(p["id"]): p.get("name") for p in people}

    rows, names = await run_db(_read)
    return [
        EscalationOut.from_row(r, names.get(str(r.get("team_member_id"))))
        for r in rows
    ]


@router.post("/properties/{property_id}/escalations/{escalation_id}/status",
             response_model=EscalationOut)
@limiter.limit("60/minute")
async def resolve_escalation(
    request: Request,
    property_id: str,
    escalation_id: str,
    body: EscalationResolveRequest,
    member=Depends(require_member("messaging")),
):
    """
    Close the loop on one escalation.

    The status is what the *agent* reads to decide whether to escalate again, so
    this route is how a hotel stops being asked the same question. The timestamp
    for each state is set here rather than by a trigger, so a row's history is
    readable without knowing what the database does behind it.
    """
    stamps = {
        "responded": "responded_at",
        "resolved": "resolved_at",
        "dismissed": "resolved_at",
    }

    def _write():
        patch = {
            "status": body.status,
            "resolution": body.resolution,
            stamps[body.status]: "now()",
            "updated_at": "now()",
        }
        if body.status in ("resolved", "dismissed"):
            patch["resolved_by"] = member.member_id
        rows = (
            get_db().table("agent_escalations").update(patch)
            .eq("id", escalation_id).eq("property_id", property_id).execute().data
        )
        return rows[0] if rows else None

    row = await run_db(_write)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Escalation not found")
    return EscalationOut.from_row(row)


# ── readiness ────────────────────────────────────────────────────────────────


@router.get("/properties/{property_id}/readiness/full", response_model=ReadinessReportOut)
@limiter.limit("60/minute")
async def get_full_readiness(
    request: Request, property_id: str, member=Depends(require_member()),
):
    """
    Every section, every room, and what each is short of.

    Beside `/readiness`, not instead of it. That route answers "why is my website
    not on Google" and the Today panel already reads it; this one answers "what
    is left to set up". Both are computed from the same rows, so they cannot
    disagree about the same hotel — which is a mistake this codebase has made
    once already, between the readiness gaps and the indexability gate.
    """
    return ReadinessReportOut(**await readiness_service.report(property_id))


@router.get("/properties/{property_id}/agent-health", response_model=ContextHealthOut)
@limiter.limit("60/minute")
async def get_agent_context_health(
    request: Request, property_id: str, member=Depends(require_member()),
):
    """
    What this hotel's AI can and cannot answer, and the evidence for each.

    §12's diagnostic. The screen a support engineer opens when a hotelier says
    "the AI told my guest it would check about breakfast" — one call that says
    whether breakfast was ever described, rather than a conversation to read and
    a guess to make.

    Readable by any member. There is nothing commercially sensitive in it: it is
    counts of the hotel's own configuration, and a receptionist who can see that
    the AI cannot answer meal questions is a receptionist who can tell somebody.
    """
    return ContextHealthOut(**await readiness_service.context_health(property_id))


@router.get("/properties/{property_id}/config-audit")
@limiter.limit("60/minute")
async def get_config_audit(
    request: Request,
    property_id: str,
    entity: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    member=Depends(require_member("rates")),
):
    """
    Who changed what, and when.

    `rates`, the same permission as making the changes it records. There is no
    write route: the service role is the only writer, so a client cannot forge a
    history that never happened.
    """
    def _read():
        query = (
            get_db().table("hotel_config_audit").select("*")
            .eq("property_id", property_id).order("at", desc=True)
        )
        if entity:
            query = query.eq("entity", entity)
        return query.limit(limit).execute().data or []

    return [
        {
            "id": r["id"],
            "entity": r["entity"],
            "entityId": str(r["entity_id"]) if r.get("entity_id") else None,
            "entityLabel": r.get("entity_label"),
            "field": r["field"],
            "oldValue": r.get("old_value"),
            "newValue": r.get("new_value"),
            "actor": r.get("actor_label"),
            "at": str(r.get("at")),
        }
        for r in await run_db(_read)
    ]


# ── rate plans ───────────────────────────────────────────────────────────────


@router.get("/properties/{property_id}/rate-plans-detail",
            response_model=list[RatePlanDetailOut])
@limiter.limit("120/minute")
async def list_rate_plan_details(
    request: Request,
    property_id: str,
    include_inactive: bool = Query(False),
    member=Depends(require_member()),
):
    """
    Every rate plan in full: contract dates, selling days, allocation, whether it
    derives its price from another plan, and its cancellation ladder.

    The existing `/rate-plans` returns what the booking drawer needs and predates
    all of that. Same reasoning as the room detail route — the screens that need
    fifteen more fields are not the screens a receptionist opens all shift.
    """
    def _read():
        query = _scoped("rate_plans", property_id).order("sort_order")
        if not include_inactive:
            query = query.eq("is_active", True)
        return query.limit(400).execute().data or []

    return [RatePlanDetailOut.from_row(r) for r in await run_db(_read)]


@router.patch("/properties/{property_id}/rate-plans/{rate_plan_id}",
              response_model=RatePlanDetailOut)
@limiter.limit("120/minute")
async def update_rate_plan(
    request: Request,
    property_id: str,
    rate_plan_id: str,
    body: RatePlanDetailRequest,
    member=Depends(require_member("rates")),
):
    """
    Amend a rate plan. The route that did not exist, and its absence was a hole.

    `POST /rate-plans` created one and nothing could change it. So a hotel that
    typed the wrong nightly rate had no way to correct it except through the ARI
    calendar, which sets a *per-night override* rather than the plan's own base —
    meaning the wrong number stayed underneath, resurfacing on every date the
    calendar did not cover. §6 asks for a clear public selling price a hotel can
    see and change; this is the route that makes the second half possible.

    **A plan may not derive from another hotel's plan, or from itself, or from a
    plan that is itself derived.** The first is checked here, the second and
    third by the database and by `rates.resolve_derived` respectively — three
    places because a derivation cycle is not a validation error, it is a pricing
    engine that does not terminate.
    """
    columns = body.to_columns()
    if not columns:
        existing = await run_db(_read_one, "rate_plans", property_id, rate_plan_id)
        if not existing:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Rate plan not found")
        return RatePlanDetailOut.from_row(existing)

    def _write():
        before = _read_one("rate_plans", property_id, rate_plan_id)
        if not before:
            return None, None

        parent_id = columns.get("derived_from_plan_id")
        if parent_id:
            if str(parent_id) == str(rate_plan_id):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "A rate cannot be a discount off itself.",
                )
            parent = _read_one("rate_plans", property_id, parent_id)
            if not parent:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    "That rate plan is not part of this hotel.",
                )
            if parent.get("derived_from_plan_id"):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "That rate is itself a discount off another one. Point at the "
                    "published rate instead.",
                )
            if str(parent.get("room_type_id")) != str(before.get("room_type_id")):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "A rate can only follow another rate for the same room.",
                )

        rows = (
            get_db().table("rate_plans").update({**columns, "updated_at": "now()"})
            .eq("id", rate_plan_id).eq("property_id", property_id).execute().data
        )
        return (rows[0] if rows else None), before

    updated, before = await run_db(_write)
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rate plan not found")

    await audit_service.record(
        property_id=property_id, partner_id=member.partner_id,
        entity="rate_plan", entity_id=rate_plan_id, entity_label=updated.get("name"),
        before=before, after=columns, actor_member=member.member_id,
    )
    return RatePlanDetailOut.from_row(updated)


# ── marketing ────────────────────────────────────────────────────────────────


@router.get("/properties/{property_id}/marketing-profile", response_model=MarketingProfileOut)
@limiter.limit("120/minute")
async def get_marketing_profile(
    request: Request, property_id: str, member=Depends(require_member()),
):
    def _read():
        rows = (
            get_db().table("marketing_profiles").select("*")
            .eq("property_id", property_id).limit(1).execute().data or []
        )
        return rows[0] if rows else None

    return MarketingProfileOut.from_row(await run_db(_read))


@router.put("/properties/{property_id}/marketing-profile", response_model=MarketingProfileOut)
@limiter.limit("60/minute")
async def set_marketing_profile(
    request: Request,
    property_id: str,
    body: MarketingProfileRequest,
    member=Depends(require_member("marketing")),
):
    """
    Who this hotel is for, in the hotel's own words.

    Every ad this product generates is filled from these fields and from the
    room being advertised — there is no model in that path, which is why the
    fields have to be here rather than inferred. See services/marketing/creatives.py.
    """
    columns = body.to_columns()

    def _write():
        rows = get_db().table("marketing_profiles").upsert(
            {**columns, "property_id": property_id, "partner_id": member.partner_id,
             "updated_at": "now()"},
            on_conflict="property_id",
        ).execute().data
        return rows[0]

    return MarketingProfileOut.from_row(await run_db(_write))


@router.get("/properties/{property_id}/audiences", response_model=list[AudienceOut])
@limiter.limit("120/minute")
async def list_audiences(request: Request, property_id: str, member=Depends(require_member())):
    def _read():
        return _scoped("ad_audiences", property_id).order("name").limit(100).execute().data or []

    return [AudienceOut.from_row(r) for r in await run_db(_read)]


@router.post("/properties/{property_id}/audiences", response_model=AudienceOut)
@limiter.limit("60/minute")
async def upsert_audience(
    request: Request,
    property_id: str,
    body: AudienceRequest,
    member=Depends(require_member("marketing")),
):
    """
    A saved audience, reusable across campaigns — §30's actual requirement.

    **The interests are stored as the hotelier's words, not as Meta interest
    ids.** Those ids come from a search endpoint StayOS cannot call until an ad
    account exists, and inventing them would target an ad at something
    arbitrary. `meta_spec` is the column that will hold what Meta accepted, once
    Meta can be asked.
    """
    if body.ageMin and body.ageMax and body.ageMax < body.ageMin:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "The age range is the wrong way round.",
        )
    columns = body.to_columns()

    def _write():
        try:
            return _upsert("ad_audiences", property_id, member.partner_id, body.id, columns)
        except Exception as exc:  # noqa: BLE001
            if "ad_audiences_property_id_name_key" in str(exc):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "You already have an audience with that name.",
                ) from exc
            raise

    row = await run_db(_write)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Audience not found")
    return AudienceOut.from_row(row)


@router.delete("/properties/{property_id}/audiences/{audience_id}", status_code=204)
@limiter.limit("60/minute")
async def delete_audience(
    request: Request, property_id: str, audience_id: str,
    member=Depends(require_member("marketing")),
):
    if not await run_db(_delete, "ad_audiences", property_id, audience_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Audience not found")


# ── ads ──────────────────────────────────────────────────────────────────────


def _ad_context(property_id: str, room_type_id: str | None, experience_id: str | None) -> dict:
    """Everything the copy templates may draw on. Nothing else is passed in."""
    db = get_db()
    prop = (
        db.table("properties").select("name,city,state")
        .eq("id", property_id).limit(1).execute().data or [{}]
    )[0]
    marketing = (
        db.table("marketing_profiles").select("*")
        .eq("property_id", property_id).limit(1).execute().data or [{}]
    )
    room = rate = experience = None
    if room_type_id:
        rooms = (
            db.table("room_types").select("id,name,description,photos,photo_meta")
            .eq("id", room_type_id).eq("property_id", property_id).limit(1).execute().data or []
        )
        room = rooms[0] if rooms else None
        if room:
            plans = (
                db.table("rate_plans").select("base_price_paise,meal_plan")
                .eq("room_type_id", room_type_id).eq("audience", "public")
                .eq("is_active", True).order("base_price_paise").limit(1).execute().data or []
            )
            rate = plans[0] if plans else None
    if experience_id:
        found = (
            db.table("experiences").select("*")
            .eq("id", experience_id).eq("property_id", property_id).limit(1).execute().data or []
        )
        experience = found[0] if found else None

    connection = (
        db.table("instagram_connections").select("status,instagram_account_id,username")
        .eq("property_id", property_id).limit(1).execute().data or []
    )
    return {
        "property": prop,
        "marketing": marketing[0] if marketing else {},
        "room": room,
        "rate": rate,
        "experience": experience,
        "connection": connection[0] if connection else None,
    }


@router.post("/properties/{property_id}/ads/drafts", response_model=AdDraftOut)
@limiter.limit("60/minute")
async def generate_ad_drafts(
    request: Request,
    property_id: str,
    body: AdCampaignRequest,
    member=Depends(require_member("marketing")),
):
    """
    Starting points for an ad, from this hotel's own facts.

    Several, not one: a hotelier choosing between three drafts edits the one
    they picked, while a hotelier handed a single draft either accepts it or
    closes the screen. Nothing is written — this is a read that composes.

    **The offer comes back with the drafts**, and it is what the copy is
    authorised against. A draft that quotes ₹3,200 is only sendable because the
    campaign's offer carries that price; `claims.check` refuses any figure the
    offer does not name, on our text and on the hotelier's edits alike.
    """
    context = await run_db(_ad_context, property_id, body.roomTypeId, body.experienceId)

    rate_paise = (context.get("rate") or {}).get("base_price_paise")
    offer: dict = {}
    if body.offerDiscountPct:
        offer["discountPct"] = body.offerDiscountPct
    if body.offerValidUntil:
        offer["validUntil"] = body.offerValidUntil.isoformat()
    if rate_paise:
        offer["pricePaise"] = rate_paise
    if (context.get("experience") or {}).get("price_paise"):
        offer["amountPaise"] = context["experience"]["price_paise"]

    facts = creatives.facts_from(
        property_row=context["property"],
        marketing=context.get("marketing"),
        room=context.get("room"),
        rate_paise=rate_paise,
        meal_plan=(context.get("rate") or {}).get("meal_plan"),
        experience=context.get("experience"),
        offer=offer,
    )
    drafts = creatives.generate(facts, destination=body.destination)

    return AdDraftOut(
        drafts=[d.as_dict() for d in drafts],
        offer=offer,
        missing=list(meta_ads._missing_credentials(context.get("connection"))),
    )


@router.get("/properties/{property_id}/ads", response_model=list[AdCampaignOut])
@limiter.limit("120/minute")
async def list_ad_campaigns(
    request: Request, property_id: str, member=Depends(require_member()),
):
    def _read():
        rows = (
            _scoped("ad_campaigns", property_id).order("created_at", desc=True)
            .limit(100).execute().data or []
        )
        if not rows:
            return [], {}
        found = (
            get_db().table("ad_creatives").select("*")
            .in_("campaign_id", [r["id"] for r in rows]).limit(400).execute().data or []
        )
        by_campaign: dict[str, list[dict]] = {}
        for c in found:
            by_campaign.setdefault(str(c["campaign_id"]), []).append(c)
        return rows, by_campaign

    rows, by_campaign = await run_db(_read)
    return [AdCampaignOut.from_row(r, by_campaign.get(str(r["id"]))) for r in rows]


@router.post("/properties/{property_id}/ads", response_model=AdCampaignOut)
@limiter.limit("60/minute")
async def upsert_ad_campaign(
    request: Request,
    property_id: str,
    body: AdCampaignRequest,
    member=Depends(require_member("marketing")),
):
    """
    Save an ad as a draft. It creates nothing on Meta — see `/publish`.

    **A published campaign's commercial fields are not editable here.** Once
    Meta holds a campaign, changing its offer in our database would make our
    record disagree with the live ad, and the record is what a hotelier reads
    when they ask what they are running.
    """
    columns = {
        "name": body.name,
        "channel": body.channel,
        "objective": body.objective,
        "destination": body.destination,
        "audience_id": body.audienceId,
        "room_type_id": body.roomTypeId,
        "experience_id": body.experienceId,
        "offer": {
            **({"discountPct": body.offerDiscountPct} if body.offerDiscountPct else {}),
            **({"validUntil": body.offerValidUntil.isoformat()} if body.offerValidUntil else {}),
        },
        "starts_on": body.startsOn.isoformat() if body.startsOn else None,
        "ends_on": body.endsOn.isoformat() if body.endsOn else None,
        "daily_budget_paise": (
            round(body.dailyBudgetRupees * 100) if body.dailyBudgetRupees is not None else None
        ),
        "created_by_member": member.member_id,
    }

    def _write():
        if body.id:
            existing = _read_one("ad_campaigns", property_id, body.id)
            if not existing:
                return None
            if existing.get("status") not in ("draft", "connected", "failed"):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "This ad is live on Meta. Pause it there before changing it here, "
                    "so what you see matches what is running.",
                )
        # The rate is stamped onto the offer at save time rather than read at
        # publish time. A campaign approved against ₹3,200 must not silently
        # start claiming ₹2,900 because somebody changed the rate afterwards.
        if body.roomTypeId:
            plans = (
                get_db().table("rate_plans").select("base_price_paise")
                .eq("room_type_id", body.roomTypeId).eq("property_id", property_id)
                .eq("audience", "public").eq("is_active", True)
                .order("base_price_paise").limit(1).execute().data or []
            )
            if plans:
                columns["offer"]["pricePaise"] = plans[0]["base_price_paise"]
        return _upsert("ad_campaigns", property_id, member.partner_id, body.id, columns)

    row = await run_db(_write)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ad not found")
    return AdCampaignOut.from_row(row)


@router.put("/properties/{property_id}/ads/{campaign_id}/creative", response_model=AdCreativeOut)
@limiter.limit("60/minute")
async def save_ad_creative(
    request: Request,
    property_id: str,
    campaign_id: str,
    body: AdCreativeRequest,
    member=Depends(require_member("marketing")),
):
    """
    The picture and the words, after the hotelier has had them.

    **`edited` is decided here, never by the client.** It is true when the copy
    differs from what the named template produced from this hotel's own facts —
    which is a question only the server can answer, and the one thing that flag
    exists to be honest about.

    Refused when the copy asserts a figure the campaign's offer does not
    authorise. The same rule broadcast bodies obey; an ad reaches more people,
    so the bar is if anything higher.
    """
    campaign = await run_db(_read_one, "ad_campaigns", property_id, campaign_id)
    if not campaign:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ad not found")

    problems = creatives.check_copy(
        body.headline or "", body.primaryText or "", campaign.get("offer") or {},
    )
    if problems:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, problems[0])

    context = await run_db(
        _ad_context, property_id, campaign.get("room_type_id"), campaign.get("experience_id"),
    )
    room = context.get("room") or {}

    media = creatives.build_media(
        fmt=body.format,
        photos=body.photos or (room.get("photos") or []),
        photo_meta=body.photoMeta or room.get("photo_meta"),
        video_url=body.videoUrl,
        instagram_media_id=body.instagramMediaId,
    )

    # Did they change it? Regenerated from the same facts and compared, so a
    # hotelier who accepted a draft verbatim is recorded as having accepted it.
    edited = True
    if body.templateKey:
        facts = creatives.facts_from(
            property_row=context["property"],
            marketing=context.get("marketing"),
            room=room or None,
            rate_paise=(context.get("rate") or {}).get("base_price_paise"),
            meal_plan=(context.get("rate") or {}).get("meal_plan"),
            experience=context.get("experience"),
            offer=campaign.get("offer") or {},
        )
        for draft in creatives.generate(facts, destination=campaign.get("destination") or "whatsapp"):
            if draft.template_key == body.templateKey:
                edited = (
                    draft.headline != (body.headline or "")
                    or draft.primary_text != (body.primaryText or "")
                )
                break

    columns = {
        "format": body.format,
        "headline": body.headline,
        "primary_text": body.primaryText,
        "cta": body.cta,
        "media": media,
        "instagram_media_id": body.instagramMediaId,
        "template_key": body.templateKey,
        "edited": edited,
        "is_primary": True,
        "campaign_id": campaign_id,
    }

    def _write():
        db = get_db()
        existing = (
            db.table("ad_creatives").select("id")
            .eq("campaign_id", campaign_id).eq("property_id", property_id)
            .eq("is_primary", True).limit(1).execute().data or []
        )
        if existing:
            rows = (
                db.table("ad_creatives").update({**columns, "updated_at": "now()"})
                .eq("id", existing[0]["id"]).eq("property_id", property_id)
                .execute().data
            )
            return rows[0] if rows else None
        return db.table("ad_creatives").insert(
            {**columns, "property_id": property_id, "partner_id": member.partner_id}
        ).execute().data[0]

    row = await run_db(_write)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ad not found")
    return AdCreativeOut.from_row(row)


@router.post("/properties/{property_id}/ads/{campaign_id}/publish", response_model=AdPublishOut)
@limiter.limit("20/hour")
async def publish_ad_campaign(
    request: Request,
    property_id: str,
    campaign_id: str,
    member=Depends(require_member("marketing")),
):
    """
    Create this ad on Meta, or say precisely why not.

    §29: *never tell the hotel "Ad published" unless the Meta API confirms it.*
    That is enforced in three places and this is the middle one — the database's
    own check constraint refuses a published row without an `external_id`, and
    the screen has a `not_connected` state rather than an error dialog.

    A hotel with no ad account gets `draft` back with the specific thing that is
    missing. Not `failed`: nothing failed, and `failed` would send somebody
    looking for a Meta rejection that does not exist.
    """
    def _read():
        campaign = _read_one("ad_campaigns", property_id, campaign_id)
        if not campaign:
            return None, None, None, None
        db = get_db()
        creative_rows = (
            db.table("ad_creatives").select("*")
            .eq("campaign_id", campaign_id).eq("is_primary", True).limit(1).execute().data or []
        )
        audience = None
        if campaign.get("audience_id"):
            audience = _read_one("ad_audiences", property_id, campaign["audience_id"])
        partner = (
            db.table("partners").select("whatsapp")
            .eq("id", campaign["partner_id"]).limit(1).execute().data or [{}]
        )[0]
        return campaign, (creative_rows[0] if creative_rows else None), audience, partner

    campaign, creative, audience, partner = await run_db(_read)
    if not campaign:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ad not found")
    if not creative:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "This ad has no picture or words yet.",
        )

    context = await run_db(
        _ad_context, property_id, campaign.get("room_type_id"), campaign.get("experience_id"),
    )

    try:
        result = await meta_ads.publish(
            campaign=campaign,
            creative=creative,
            audience=audience,
            connection=context.get("connection"),
            whatsapp_number=(partner or {}).get("whatsapp"),
        )
    except NotImplementedError:
        # Unreachable while no hotel has ad credentials — `publish` refuses
        # earlier — and caught anyway, because the day that changes this route
        # must not 500 at a hotelier who pressed a button.
        logger.warning("ad publish reached the unimplemented Meta call", exc_info=True)
        result = meta_ads.PublishResult(
            status="draft",
            detail="Publishing to Meta is not switched on yet.",
            missing=("ads_permission",),
        )

    def _record():
        patch: dict = {"last_error": result.detail if not result.ok else None,
                       "updated_at": "now()"}
        if result.ok and result.external_id:
            patch.update({"status": result.status, "external_id": result.external_id,
                          "last_synced_at": "now()"})
        get_db().table("ad_campaigns").update(patch).eq("id", campaign_id).eq(
            "property_id", property_id
        ).execute()

    await run_db(_record)
    return AdPublishOut(**result.as_dict())


@router.delete("/properties/{property_id}/ads/{campaign_id}", status_code=204)
@limiter.limit("60/minute")
async def delete_ad_campaign(
    request: Request, property_id: str, campaign_id: str,
    member=Depends(require_member("marketing")),
):
    """
    Delete a draft. A campaign Meta knows about cannot be deleted here.

    Removing our row would leave an ad running that nothing in StayOS can see —
    the hotelier would be spending money on something their dashboard says does
    not exist.
    """
    existing = await run_db(_read_one, "ad_campaigns", property_id, campaign_id)
    if not existing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ad not found")
    if existing.get("external_id"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This ad exists on Meta. Stop it there first — deleting it here would "
            "leave it running with nothing to show you.",
        )
    await run_db(_delete, "ad_campaigns", property_id, campaign_id)
