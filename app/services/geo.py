"""
Lightweight static geo reference — city → (country, primary IATA).

Two jobs:
  1. IATA fallback for flight search, so resolution never hinges on a single
     live autosuggest call (the old cause of embarrassing "no flights").
  2. Country gating, so India-only trains and cross-country buses are never
     attempted/shown (no "train from Delhi to New York / Dubai").

This is intentionally NOT exhaustive. The Indian set is biased toward airports
+ common tourist towns; the foreign set is biased toward routes Indian
travellers actually book. Unknown cities resolve to ``None`` and are given the
benefit of the doubt by callers (treated as possibly-domestic) so we never wrongly
block a valid intra-India route for an obscure town. Live autosuggest still
handles the long tail for IATA codes.

country codes are ISO-3166 alpha-2; "IN" = India.
"""
from __future__ import annotations

import re
from typing import Optional

# ─── Normalisation ───────────────────────────────────────────────────────────

# Common alias → canonical key (both already normalised: lowercase, alnum+space)
_ALIASES = {
    "bombay": "mumbai",
    "bangalore": "bengaluru",
    "calcutta": "kolkata",
    "madras": "chennai",
    "cochin": "kochi",
    "trivandrum": "thiruvananthapuram",
    "vizag": "visakhapatnam",
    "pondicherry": "puducherry",
    "gurgaon": "gurugram",
    "panjim": "goa",
    "panaji": "goa",
    "calicut": "kozhikode",
    "trichy": "tiruchirapalli",
    "allahabad": "prayagraj",
    "benares": "varanasi",
    "mysore": "mysuru",
    "mangalore": "mangaluru",
    "belgaum": "belagavi",
    "new delhi": "delhi",
    "ncr": "delhi",
    "nyc": "new york",
    "new york city": "new york",
    "bangkok city": "bangkok",
    "ho chi minh city": "ho chi minh",
    "saigon": "ho chi minh",
    "denpasar": "bali",
    "washington dc": "washington",
}


def normalize(city: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace, resolve aliases."""
    s = (city or "").lower().strip()
    s = re.sub(r"[^a-z0-9\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return _ALIASES.get(s, s)


# ─── Reference data ──────────────────────────────────────────────────────────
# value = (country_code, primary_iata_or_None)

# Indian cities — airports where one exists, else IATA None (still country "IN").
_INDIA: dict[str, Optional[str]] = {
    # metros + major airports
    "delhi": "DEL", "mumbai": "BOM", "bengaluru": "BLR", "chennai": "MAA",
    "kolkata": "CCU", "hyderabad": "HYD", "goa": "GOI", "pune": "PNQ",
    "ahmedabad": "AMD", "jaipur": "JAI", "lucknow": "LKO", "kochi": "COK",
    "thiruvananthapuram": "TRV", "srinagar": "SXR", "leh": "IXL", "guwahati": "GAU",
    "bhubaneswar": "BBI", "nagpur": "NAG", "indore": "IDR", "coimbatore": "CJB",
    "varanasi": "VNS", "amritsar": "ATQ", "patna": "PAT", "ranchi": "IXR",
    "raipur": "RPR", "vadodara": "BDQ", "surat": "STV", "udaipur": "UDR",
    "jodhpur": "JDH", "dehradun": "DED", "chandigarh": "IXC", "bagdogra": "IXB",
    "dibrugarh": "DIB", "imphal": "IMF", "agartala": "IXA", "port blair": "IXZ",
    "mangaluru": "IXE", "madurai": "IXM", "tiruchirapalli": "TRZ",
    "vijayawada": "VGA", "visakhapatnam": "VTZ", "aurangabad": "IXU",
    "jammu": "IXJ", "gwalior": "GWL", "jabalpur": "JLR", "bhopal": "BHO",
    "rajkot": "RAJ", "jamnagar": "JGA", "kannur": "CNN", "kozhikode": "CCJ",
    "hubli": "HBX", "belagavi": "IXG", "shimla": "SLV", "kullu": "KUU",
    "dharamshala": "DHM", "pantnagar": "PGH", "jaisalmer": "JSA", "bikaner": "BKB",
    "agra": "AGR", "kanpur": "KNU", "gorakhpur": "GOP", "prayagraj": "IXD",
    "durgapur": "RDP", "jorhat": "JRH", "dimapur": "DMU", "shillong": "SHL",
    "aizawl": "AJL", "silchar": "IXS", "gaya": "GAY", "mysuru": "MYQ",
    "tirupati": "TIR", "rajahmundry": "RJA", "nashik": "ISK", "tezpur": "TEZ",
    "gurugram": None, "noida": None, "puducherry": "PNY", "ajmer": None,
    # popular tourist towns without their own airport (country gating only)
    "manali": None, "mussoorie": None, "nainital": None, "rishikesh": None,
    "haridwar": None, "mcleodganj": None, "kasol": None, "spiti": None,
    "ladakh": "IXL", "darjeeling": "IXB", "gangtok": "PYG", "munnar": None,
    "alleppey": None, "varkala": None, "hampi": None, "gokarna": None,
    "pushkar": None, "mount abu": None, "ooty": None, "kodaikanal": None,
    "coorg": None, "wayanad": None, "auli": None, "kufri": None, "lansdowne": None,
    "khajuraho": "HJR", "rann of kutch": None, "tawang": None, "ziro": None,
}

# Foreign cities — biased toward routes Indians book. Country code + primary IATA.
_FOREIGN: dict[str, tuple[str, Optional[str]]] = {
    "dubai": ("AE", "DXB"), "abu dhabi": ("AE", "AUH"), "sharjah": ("AE", "SHJ"),
    "doha": ("QA", "DOH"), "singapore": ("SG", "SIN"), "bangkok": ("TH", "BKK"),
    "phuket": ("TH", "HKT"), "pattaya": ("TH", None), "krabi": ("TH", "KBV"),
    "kuala lumpur": ("MY", "KUL"), "bali": ("ID", "DPS"), "jakarta": ("ID", "CGK"),
    "kathmandu": ("NP", "KTM"), "pokhara": ("NP", "PKR"), "colombo": ("LK", "CMB"),
    "male": ("MV", "MLE"), "maldives": ("MV", "MLE"), "dhaka": ("BD", "DAC"),
    "london": ("GB", "LHR"), "manchester": ("GB", "MAN"), "edinburgh": ("GB", "EDI"),
    "paris": ("FR", "CDG"), "nice": ("FR", "NCE"), "new york": ("US", "JFK"),
    "los angeles": ("US", "LAX"), "san francisco": ("US", "SFO"),
    "chicago": ("US", "ORD"), "washington": ("US", "IAD"), "boston": ("US", "BOS"),
    "seattle": ("US", "SEA"), "dallas": ("US", "DFW"), "houston": ("US", "IAH"),
    "atlanta": ("US", "ATL"), "miami": ("US", "MIA"), "las vegas": ("US", "LAS"),
    "toronto": ("CA", "YYZ"), "vancouver": ("CA", "YVR"), "sydney": ("AU", "SYD"),
    "melbourne": ("AU", "MEL"), "tokyo": ("JP", "NRT"), "osaka": ("JP", "KIX"),
    "seoul": ("KR", "ICN"), "hong kong": ("HK", "HKG"), "shanghai": ("CN", "PVG"),
    "beijing": ("CN", "PEK"), "istanbul": ("TR", "IST"), "frankfurt": ("DE", "FRA"),
    "munich": ("DE", "MUC"), "berlin": ("DE", "BER"), "amsterdam": ("NL", "AMS"),
    "rome": ("IT", "FCO"), "milan": ("IT", "MXP"), "venice": ("IT", "VCE"),
    "madrid": ("ES", "MAD"), "barcelona": ("ES", "BCN"), "zurich": ("CH", "ZRH"),
    "geneva": ("CH", "GVA"), "vienna": ("AT", "VIE"), "dublin": ("IE", "DUB"),
    "lisbon": ("PT", "LIS"), "moscow": ("RU", "SVO"), "cairo": ("EG", "CAI"),
    "nairobi": ("KE", "NBO"), "johannesburg": ("ZA", "JNB"), "cape town": ("ZA", "CPT"),
    "mauritius": ("MU", "MRU"), "muscat": ("OM", "MCT"), "riyadh": ("SA", "RUH"),
    "jeddah": ("SA", "JED"), "kuwait": ("KW", "KWI"), "bahrain": ("BH", "BAH"),
    "manama": ("BH", "BAH"), "baku": ("AZ", "GYD"), "tashkent": ("UZ", "TAS"),
    "almaty": ("KZ", "ALA"), "hanoi": ("VN", "HAN"), "ho chi minh": ("VN", "SGN"),
    "manila": ("PH", "MNL"), "brussels": ("BE", "BRU"), "copenhagen": ("DK", "CPH"),
    "stockholm": ("SE", "ARN"), "oslo": ("NO", "OSL"), "helsinki": ("FI", "HEL"),
    "athens": ("GR", "ATH"), "prague": ("CZ", "PRG"), "budapest": ("HU", "BUD"),
    "warsaw": ("PL", "WAW"), "auckland": ("NZ", "AKL"),
}


# ─── Public helpers ──────────────────────────────────────────────────────────

# Reverse index: IATA → country, for callers that only hold airport codes.
_IATA_COUNTRY: dict[str, str] = {}
for _city, _code in _INDIA.items():
    if _code:
        _IATA_COUNTRY.setdefault(_code, "IN")
for _city, (_cc, _code) in _FOREIGN.items():
    if _code:
        _IATA_COUNTRY.setdefault(_code, _cc)


def country_of_iata(iata: str) -> Optional[str]:
    """ISO country code for an IATA airport code, or None if unknown."""
    return _IATA_COUNTRY.get((iata or "").upper())


def is_domestic_iata(from_iata: str, to_iata: str) -> bool:
    """
    True unless one side is a *known* foreign airport. Unknown codes default to
    domestic (the common EaseMyTrip case) so we never wrongly flag a valid Indian
    route as international.
    """
    fc, tc = country_of_iata(from_iata), country_of_iata(to_iata)
    foreign = (fc is not None and fc != "IN") or (tc is not None and tc != "IN")
    return not foreign


def country_of(city: str) -> Optional[str]:
    """ISO country code for a city, or None if unknown. Indian cities → 'IN'."""
    key = normalize(city)
    if key in _INDIA:
        return "IN"
    f = _FOREIGN.get(key)
    return f[0] if f else None


def iata_of(city: str) -> Optional[str]:
    """Primary IATA airport code for a city, or None if unknown / no airport."""
    key = normalize(city)
    if key in _INDIA:
        return _INDIA[key]
    f = _FOREIGN.get(key)
    return f[1] if f else None


def is_known_foreign(city: str) -> bool:
    """True only when the city is confidently NOT in India (known non-IN country)."""
    c = country_of(city)
    return c is not None and c != "IN"


def cross_country(a: str, b: str) -> bool:
    """
    True only when both cities are known and in different countries.
    Unknown cities → False (benefit of the doubt; don't over-block).
    """
    ca, cb = country_of(a), country_of(b)
    return ca is not None and cb is not None and ca != cb


def india_abroad(a: str, b: str) -> bool:
    """
    True when exactly one endpoint is in India and the other is known-foreign.

    Used to gate ground transport (bus): there is no sensible Delhi→Dubai bus,
    but intra-Europe cross-border buses (e.g. Berlin→Paris on FlixBus) must stay
    allowed, so this is narrower than ``cross_country``.
    """
    ca, cb = country_of(a), country_of(b)
    in_a, in_b = ca == "IN", cb == "IN"
    foreign_a = ca is not None and ca != "IN"
    foreign_b = cb is not None and cb != "IN"
    return (in_a and foreign_b) or (in_b and foreign_a)
