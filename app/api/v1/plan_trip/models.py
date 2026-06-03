from pydantic import BaseModel


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


class PlanJourneyRequest(BaseModel):
    origin: str
    destination: str
    transport_modes: list[str] = []
    travelers: int = 1
    budget: str = "mid"
    departure_date: str | None = None
    stay_nights: int = 2


class PlanRequest(BaseModel):
    """
    Unified request — returns both the transport plan and day-by-day itinerary
    in a single API call by running plan-journey + plan-trip in parallel.
    """
    origin: str
    destination: str
    departure_date: str | None = None   # "YYYY-MM-DD" — travel + itinerary start date
    days: int = 3                        # itinerary length
    travelers: int = 1
    budget: str = "mid"                  # "low" | "mid" | "high"
    interests: list[str] = []
    transport_modes: list[str] = []      # [] = Gemini picks best mode
    stay_nights: int | None = None       # defaults to days - 1
