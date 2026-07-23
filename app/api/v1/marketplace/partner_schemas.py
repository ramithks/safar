"""Request/response models for the privileged partner operations."""
from typing import Any, Literal

from pydantic import BaseModel, Field


class GapItem(BaseModel):
    """One unmet publish requirement, shaped for the editor's checklist."""

    field: str
    label: str


class Ok(BaseModel):
    ok: bool = True


class CounterRequest(BaseModel):
    """A view or lead on a public item. `id` is validated as a real row by the
    RPC, which no-ops on an unknown one."""

    kind: Literal["listing", "trip"]
    id: str = Field(min_length=1, max_length=64)
    metric: Literal["views", "leads"] = "views"


class SeatsRequest(BaseModel):
    # An upper bound stops a typo from creating a five-figure trip; the lower
    # bound against seats already sold is enforced in the endpoint, where the
    # stored row is available.
    seats_total: int = Field(ge=0, le=10_000)


class StatusResponse(BaseModel):
    """
    Shared shape for every status transition.

    `ok=False` with `gaps` or `needs_plan` is a normal, expected answer — the
    listing simply isn't ready — so these are 200s, not errors. Only genuine
    failures raise HTTPException.
    """

    ok: bool
    status: str | None = None
    error: str | None = None
    gaps: list[GapItem] | None = None
    needs_plan: bool = False
    listing: dict[str, Any] | None = None
