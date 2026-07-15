from __future__ import annotations
from typing import List, Optional
from pydantic import BaseModel, Field


class FlightSearchRequest(BaseModel):
    origin: str = Field(..., examples=["Delhi"])
    destination: str = Field(..., examples=["Goa"])
    date: str = Field(..., description="YYYY-MM-DD", examples=["2026-06-15"])
    adults: int = Field(1, ge=1, le=9)
    cabin_class: str = Field("ECONOMY", examples=["ECONOMY", "BUSINESS", "FIRST"])
    sort: str = Field("CHEAPEST", description="CHEAPEST | FASTEST | BEST")
    budget: Optional[str] = Field(
        None,
        description="low | mid | high — when set, dictates sort (low→cheapest, mid→best, high→fastest) and overrides `sort`.",
    )
    from_iata: Optional[str] = Field(
        None,
        description="Pre-resolved origin IATA from /autocomplete/airports — skips re-resolution.",
        examples=["DEL"],
    )
    to_iata: Optional[str] = Field(
        None,
        description="Pre-resolved destination IATA from /autocomplete/airports — skips re-resolution.",
        examples=["GOI"],
    )
    nonstop_only: bool = False


class HotelFilters(BaseModel):
    """
    Canonical hard filters applied to the merged multi-source result list.
    Full vocabulary at GET /search/hotels/filters. Unknown values are dropped
    server-side; explicit filters override the persona/budget soft heuristics
    they overlap (property types, price band, sort).
    """
    property_types: Optional[List[str]] = Field(
        None,
        description="hostel | homestay | guesthouse | hotel | resort | apartment | villa | vacation_home | bnb",
        examples=[["hostel", "homestay"]],
    )
    price_min: Optional[float] = Field(None, ge=0, description="Min price per night (search currency)")
    price_max: Optional[float] = Field(None, ge=0, description="Max price per night (search currency)")
    min_review_score: Optional[float] = Field(
        None, ge=0, le=10,
        description="Guest review score floor on a /10 scale (good 7+, very good 8+, wonderful 9+)",
    )
    star_ratings: Optional[List[int]] = Field(None, description="Allowed star classes, e.g. [4, 5]")
    facilities: Optional[List[str]] = Field(
        None,
        description="Canonical facility keys (see /search/hotels/filters), e.g. pool, wifi, parking, spa, sea_view",
        examples=[["pool", "wifi"]],
    )
    meals: Optional[List[str]] = Field(
        None, description="breakfast_included | half_board | full_board | all_inclusive",
    )
    free_cancellation: Optional[bool] = None
    no_prepayment: Optional[bool] = None
    breakfast_included: Optional[bool] = None
    couple_friendly: Optional[bool] = None
    pet_friendly: Optional[bool] = None
    max_distance_km: Optional[float] = Field(None, gt=0, description="Max distance from the destination centre")
    sort: Optional[str] = Field(
        None,
        description="recommended | price_asc | price_desc | rating — overrides the budget→sort mapping",
    )


class HotelSearchRequest(BaseModel):
    destination: str = Field(..., examples=["Manali"])
    checkin: str = Field(..., description="YYYY-MM-DD", examples=["2026-06-15"])
    checkout: str = Field(..., description="YYYY-MM-DD", examples=["2026-06-17"])
    rooms: int = Field(1, ge=1, le=5)
    adults: int = Field(2, ge=1, le=9)
    children: int = Field(0, ge=0, le=4)
    budget: str = Field("mid", description="low | mid | high")
    travel_type: Optional[str] = Field(
        None,
        description="solo | couple | friends | family. Drives property type "
        "(solo+low→hostels, couple/family→no hostels, high→resorts/hotels). "
        "Derived from adults when omitted.",
    )
    sort: str = Field("recommended", description="recommended | price_asc | price_desc | rating")
    filters: Optional[HotelFilters] = Field(
        None, description="Hard filters over the merged results — see GET /search/hotels/filters",
    )


class BusSearchRequest(BaseModel):
    origin: str = Field(..., examples=["Delhi"])
    destination: str = Field(..., examples=["Manali"])
    date: str = Field(..., description="YYYY-MM-DD", examples=["2026-06-15"])
    travelers: int = Field(1, ge=1, le=9)
    sort: str = Field("cheapest", description="cheapest | earliest | latest | rating")
    limit: int = Field(15, ge=1, le=50)


class TrainSearchRequest(BaseModel):
    origin: str = Field(..., examples=["Delhi"])
    destination: str = Field(..., examples=["Jaipur"])
    date: str = Field(..., description="YYYY-MM-DD", examples=["2026-06-15"])
    sort: str = Field("cheapest", description="cheapest | fastest | earliest")
    limit: int = Field(15, ge=1, le=50)


class AllSearchRequest(BaseModel):
    origin: str = Field(..., examples=["Delhi"])
    destination: str = Field(..., examples=["Manali"])
    date: str = Field(..., description="YYYY-MM-DD travel/departure date", examples=["2026-06-15"])
    travelers: int = Field(1, ge=1, le=9)
    modes: Optional[List[str]] = Field(
        None,
        description='Subset of ["flight","bus","train"]. Omit for all three.',
        examples=[["bus", "train"]],
    )
    checkin: Optional[str] = Field(None, description="YYYY-MM-DD — include to also search hotels")
    checkout: Optional[str] = Field(None, description="YYYY-MM-DD checkout for hotel search")
    rooms: int = Field(1, ge=1, le=5)
