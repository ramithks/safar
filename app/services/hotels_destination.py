"""
Canonical destination normalization for hotel search — the hotel-side equivalent
of the flight IATA resolver.

Hotel scrapers take a raw city string (EaseMyTrip especially has no autocomplete),
so aliases, neighbourhoods, spelling variants and hill-station towns silently miss.
``normalize_destination`` collapses those to one canonical query fed to every
source, and classifies the destination so the distance filter can adapt its radius.

Reuses ``app/services/geo.py`` for alias resolution + country lookup; adds only the
hotel-specific aliases (neighbourhoods/valleys) and a destination-class map that geo
doesn't carry.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services import geo


@dataclass
class Destination:
    query: str          # canonical city string passed to every source
    display_name: str   # human label (original casing)
    country: str | None # ISO-2 country code, or None if unknown
    dest_class: str     # metro | beach | hill | town


# Hotel-specific aliases geo doesn't cover — neighbourhoods/valleys/villages →
# the bookable parent town. (geo.normalize already maps bombay→mumbai, panaji→goa…)
_HOTEL_ALIASES = {
    "parvati valley": "kasol", "tosh": "kasol", "kalga": "kasol",
    "kheerganga": "kasol", "chalal": "kasol", "old manali": "manali",
    "new manali": "manali", "solang": "manali", "old goa": "goa",
    "mcleod ganj": "mcleodganj", "triund": "mcleodganj", "bhagsu": "mcleodganj",
    "tapovan": "rishikesh", "laxman jhula": "rishikesh", "ram jhula": "rishikesh",
    "old delhi": "delhi", "new delhi": "delhi",
}

# Destination-class sets — drive the adaptive distance radius (Destination.dest_class).
_BEACH = {
    "goa", "north goa", "south goa", "gokarna", "varkala", "kovalam",
    "puducherry", "pondicherry", "port blair", "havelock", "neil island",
    "diu", "daman", "alibaug", "murudeshwar", "mahabalipuram", "puri",
    "kanyakumari", "tarkarli", "malvan", "kashid", "ganpatipule", "digha",
    # foreign beaches
    "bali", "phuket", "krabi", "koh samui", "langkawi", "pattaya", "maldives",
    "male", "mauritius", "seychelles", "nadi",
}
_HILL = {
    "manali", "shimla", "mussoorie", "nainital", "kasol", "mcleodganj",
    "dharamshala", "darjeeling", "gangtok", "ooty", "munnar", "coorg",
    "kodaikanal", "leh", "ladakh", "spiti", "dalhousie", "kufri", "chail",
    "kasauli", "lansdowne", "auli", "chopta", "almora", "ranikhet", "kausani",
    "mukteshwar", "binsar", "rishikesh", "haridwar", "tawang", "ziro",
    "bir billing", "palampur", "chamba", "khajjiar", "kinnaur", "sangla",
    "chitkul", "jibhi", "tirthan", "mount abu", "matheran", "mahabaleshwar",
    "panchgani", "lonavala", "wayanad", "chikmagalur", "sakleshpur", "gulmarg",
    "pahalgam", "sonmarg", "pokhara",
}
_METRO = {
    "delhi", "mumbai", "bengaluru", "chennai", "kolkata", "hyderabad", "pune",
    "ahmedabad", "jaipur", "surat", "lucknow", "chandigarh", "kochi",
}


def _classify(key: str, country: str | None) -> str:
    if key in _BEACH:
        return "beach"
    if key in _HILL:
        return "hill"
    if key in _METRO:
        return "metro"
    if country and country != "IN":
        return "metro"          # foreign cities sprawl → generous radius
    return "town"


def normalize_destination(city: str) -> Destination:
    """Resolve a raw destination string to a canonical, classified Destination."""
    raw = (city or "").strip()
    key = geo.normalize(raw)                 # geo aliases (bombay→mumbai, panaji→goa)
    key = _HOTEL_ALIASES.get(key, key)       # hotel-specific neighbourhood aliases
    country = geo.country_of(key)
    dest_class = _classify(key, country)
    return Destination(
        query=key or raw.lower(),
        display_name=raw.title() if raw else key.title(),
        country=country,
        dest_class=dest_class,
    )


# Adaptive distance radius (km) per destination class. Sparse-result widening is
# handled by the caller (it re-runs with a wider cap when too few hotels survive).
_DISTANCE_KM = {"metro": 18.0, "beach": 28.0, "hill": 10.0, "town": 14.0}


def distance_km_for(dest_class: str) -> float:
    """Base max-distance (km) for a destination class."""
    return _DISTANCE_KM.get(dest_class, 14.0)
