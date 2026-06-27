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
