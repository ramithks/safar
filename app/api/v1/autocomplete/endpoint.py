"""
Autocomplete endpoints — the shared, fast type-ahead behind both search lanes.

Routes:
  GET /api/v1/autocomplete/airports?q=del        — flight origins/destinations
  GET /api/v1/autocomplete/destinations?q=mana    — stay destinations

Both return ranked `{city, country, country_code, iata}` rows. For flights, feed
the picked row's `iata` to POST /search/flights as from_iata/to_iata to skip
re-resolution; for stays, feed `city` to POST /search/hotels as `destination`.
"""
from fastapi import APIRouter, Depends, Query

from app.deps.auth import require_user
from app.services.autocomplete import suggest_airports, suggest_destinations

router = APIRouter(prefix="/autocomplete", tags=["autocomplete"])


@router.get("/airports")
async def autocomplete_airports(
    q: str = Query(..., min_length=2, description="Partial city or airport name", examples=["del"]),
    limit: int = Query(8, ge=1, le=20),
    _user=Depends(require_user),
):
    """Airport/city suggestions for flight search (every row has an IATA code)."""
    suggestions = await suggest_airports(q, limit=limit)
    return {"query": q, "type": "airport", "total": len(suggestions), "suggestions": suggestions}


@router.get("/destinations")
async def autocomplete_destinations(
    q: str = Query(..., min_length=2, description="Partial city or town name", examples=["mana"]),
    limit: int = Query(8, ge=1, le=20),
    _user=Depends(require_user),
):
    """Destination suggestions for stay search (cities need not have an airport)."""
    suggestions = await suggest_destinations(q, limit=limit)
    return {"query": q, "type": "destination", "total": len(suggestions), "suggestions": suggestions}
