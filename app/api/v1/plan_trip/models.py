from datetime import date as _date

from pydantic import BaseModel, field_validator


def _validate_not_past_date(v: str | None) -> str | None:
    if not v:
        return v
    try:
        d = _date.fromisoformat(v)
    except ValueError:
        raise ValueError("departure_date must be in YYYY-MM-DD format")
    if d < _date.today():
        raise ValueError(f"departure_date {v} is in the past — use today or a future date")
    return v


class PlanTripRequest(BaseModel):
    destination: str
    days: int = 3
    budget: str = "mid"
    travelers: int = 2
    interests: list[str] = []
    start_date: str | None = None
    end_date: str | None = None
    is_public: bool = True
    skip_user_trips: bool = False
    include_itinerary: bool = True

    @field_validator("destination")
    @classmethod
    def validate_destination(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) < 2:
            raise ValueError("Destination must be at least 2 characters")
        if len(v) > 100:
            raise ValueError("Destination must be 100 characters or fewer")
        return v

    @field_validator("days")
    @classmethod
    def validate_days(cls, v: int) -> int:
        if not (1 <= v <= 30):
            raise ValueError("Days must be between 1 and 30")
        return v

    @field_validator("travelers")
    @classmethod
    def validate_travelers(cls, v: int) -> int:
        if not (1 <= v <= 20):
            raise ValueError("Travelers must be between 1 and 20")
        return v

    @field_validator("budget")
    @classmethod
    def validate_budget(cls, v: str) -> str:
        allowed = {"low", "mid", "high"}
        if v not in allowed:
            raise ValueError(f"Budget must be one of: {', '.join(sorted(allowed))}")
        return v


class PlanJourneyRequest(BaseModel):
    origin: str
    destination: str
    transport_modes: list[str] = []
    travelers: int = 1
    budget: str = "mid"
    departure_date: str | None = None
    stay_nights: int = 2

    @field_validator("departure_date")
    @classmethod
    def validate_departure_date(cls, v: str | None) -> str | None:
        return _validate_not_past_date(v)

    @field_validator("travelers")
    @classmethod
    def validate_travelers(cls, v: int) -> int:
        if not (1 <= v <= 20):
            raise ValueError("Travelers must be between 1 and 20")
        return v


class PlanRequest(BaseModel):
    """
    Unified request — transport plan + day-by-day itinerary in one call.

    Minimal required fields: origin, destination, departure_date, days.
    Everything else has sensible defaults.
    """
    origin: str
    destination: str
    departure_date: str | None = None   # "YYYY-MM-DD"
    days: int = 3
    travelers: int = 1
    budget: str = "mid"                 # "low" | "mid" | "high"
    interests: list[str] = []

    @field_validator("departure_date")
    @classmethod
    def validate_departure_date(cls, v: str | None) -> str | None:
        return _validate_not_past_date(v)

    @field_validator("origin", "destination")
    @classmethod
    def validate_city(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) < 2:
            raise ValueError("Must be at least 2 characters")
        if len(v) > 100:
            raise ValueError("Must be 100 characters or fewer")
        return v

    @field_validator("days")
    @classmethod
    def validate_days(cls, v: int) -> int:
        if not (1 <= v <= 30):
            raise ValueError("Days must be between 1 and 30")
        return v

    @field_validator("travelers")
    @classmethod
    def validate_travelers(cls, v: int) -> int:
        if not (1 <= v <= 20):
            raise ValueError("Travelers must be between 1 and 20")
        return v

    @field_validator("budget")
    @classmethod
    def validate_budget(cls, v: str) -> str:
        allowed = {"low", "mid", "high"}
        if v not in allowed:
            raise ValueError(f"Budget must be one of: {', '.join(sorted(allowed))}")
        return v
