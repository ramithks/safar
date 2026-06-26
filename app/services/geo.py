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
    # ── India ──
    "bombay": "mumbai",
    "bangalore": "bengaluru",
    "calcutta": "kolkata",
    "madras": "chennai",
    "cochin": "kochi",
    "kochin": "kochi",
    "ernakulam": "kochi",
    "trivandrum": "thiruvananthapuram",
    "vizag": "visakhapatnam",
    "pondicherry": "puducherry",
    "pondy": "puducherry",
    "gurgaon": "gurugram",
    "greater noida": "noida",
    "navi mumbai": "mumbai",
    "thane": "mumbai",
    "secunderabad": "hyderabad",
    "panjim": "goa",
    "panaji": "goa",
    "vasco": "goa",
    "margao": "goa",
    "calangute": "goa",
    "baga": "goa",
    "anjuna": "goa",
    "calicut": "kozhikode",
    "trichy": "tiruchirapalli",
    "allahabad": "prayagraj",
    "benares": "varanasi",
    "banaras": "varanasi",
    "kashi": "varanasi",
    "mysore": "mysuru",
    "mangalore": "mangaluru",
    "belgaum": "belagavi",
    "new delhi": "delhi",
    "old delhi": "delhi",
    "ncr": "delhi",
    "madikeri": "coorg",
    "kodagu": "coorg",
    "leh ladakh": "leh",
    "mcleod ganj": "mcleodganj",
    # ── World ──
    "nyc": "new york",
    "new york city": "new york",
    "la": "los angeles",
    "sf": "san francisco",
    "vegas": "las vegas",
    "washington dc": "washington",
    "bangkok city": "bangkok",
    "ho chi minh city": "ho chi minh",
    "hcmc": "ho chi minh",
    "saigon": "ho chi minh",
    "denpasar": "bali",
    "ubud": "bali",
    "kuta": "bali",
    "peking": "beijing",
    "rangoon": "yangon",
    "kiev": "kyiv",
    "st petersburg": "saint petersburg",
    "cologne": "koln",
    "sao paulo city": "sao paulo",
}


def normalize(city: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace, resolve aliases."""
    s = (city or "").lower().strip()
    s = re.sub(r"[^a-z0-9\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return _ALIASES.get(s, s)


# ─── Reference data ──────────────────────────────────────────────────────────
# value = (country_code, primary_iata_or_None)

# Indian cities — IATA is the city's own airport, or the *nearest serviceable*
# airport for airport-less towns (so flight search routes to a real hub). Country
# gating keys off membership in this dict, NOT the IATA value, so a borrowed
# nearest-airport code never mislabels the town's country.
_INDIA: dict[str, Optional[str]] = {
    # ── metros + cities with their own airport ──
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
    "puducherry": "PNY", "bhuj": "BHJ", "diu": "DIU", "kadapa": "CDP",
    "salem": "SXV", "tuticorin": "TCR", "kolhapur": "KLH", "shirdi": "SAG",
    "darbhanga": "DBR", "deoghar": "DGH", "bareilly": "BEK", "kushinagar": "KBK",
    "jharsuguda": "JRG", "kishangarh": "KQH", "sindhudurg": "RTC", "itanagar": "HGI",
    "pasighat": "IXT", "kurnool": "KJB",
    # ── airport-less towns → nearest serviceable airport ──
    "gurugram": "DEL", "noida": "DEL", "ajmer": "KQH",
    "manali": "KUU", "mussoorie": "DED", "nainital": "PGH", "rishikesh": "DED",
    "haridwar": "DED", "mcleodganj": "DHM", "kasol": "KUU", "spiti": "KUU",
    "ladakh": "IXL", "darjeeling": "IXB", "gangtok": "PYG", "munnar": "COK",
    "alleppey": "COK", "varkala": "TRV", "hampi": "HBX", "gokarna": "IXE",
    "pushkar": "KQH", "mount abu": "UDR", "ooty": "CJB", "kodaikanal": "IXM",
    "coorg": "MYQ", "wayanad": "CCJ", "auli": "DED", "kufri": "SLV",
    "lansdowne": "DED", "khajuraho": "HJR", "rann of kutch": "BHJ",
    "tawang": "TEZ", "ziro": "HGI", "lonavala": "BOM", "mahabaleshwar": "PNQ",
    "panchgani": "PNQ", "matheran": "BOM", "igatpuri": "ISK", "saputara": "STV",
    "silvassa": "STV", "kasauli": "IXC", "dalhousie": "DHM", "khajjiar": "DHM",
    "chail": "SLV", "palampur": "DHM", "bir billing": "DHM", "chamba": "DHM",
    "kinnaur": "KUU", "sangla": "KUU", "chitkul": "KUU", "jibhi": "KUU",
    "tirthan": "KUU", "kheerganga": "KUU", "tosh": "KUU", "malana": "KUU",
    "mandi": "KUU", "chopta": "DED", "kedarnath": "DED", "badrinath": "DED",
    "harsil": "DED", "chakrata": "DED", "almora": "PGH", "ranikhet": "PGH",
    "kausani": "PGH", "binsar": "PGH", "mukteshwar": "PGH", "jim corbett": "PGH",
    "ranthambore": "JAI", "sariska": "JAI", "bharatpur": "AGR", "mathura": "AGR",
    "vrindavan": "AGR", "fatehpur sikri": "AGR", "bundi": "JAI", "chittorgarh": "UDR",
    "kaziranga": "JRH", "majuli": "JRH", "cherrapunji": "SHL", "dawki": "SHL",
    "bomdila": "TEZ", "mahabalipuram": "MAA", "kanyakumari": "TRZ",
    "rameshwaram": "IXM", "yercaud": "SXV", "chikmagalur": "HBX",
    "sakleshpur": "IXE", "dwarka": "JGA", "somnath": "DIU", "gir": "DIU",
    "havelock": "IXZ", "neil island": "IXZ",
}

# Foreign cities — major world cities + routes Indians book. Country code + primary IATA.
_FOREIGN: dict[str, tuple[str, Optional[str]]] = {
    # ── Middle East ──
    "dubai": ("AE", "DXB"), "abu dhabi": ("AE", "AUH"), "sharjah": ("AE", "SHJ"),
    "ras al khaimah": ("AE", "RKT"), "doha": ("QA", "DOH"), "muscat": ("OM", "MCT"),
    "salalah": ("OM", "SLL"), "riyadh": ("SA", "RUH"), "jeddah": ("SA", "JED"),
    "dammam": ("SA", "DMM"), "kuwait": ("KW", "KWI"), "bahrain": ("BH", "BAH"),
    "manama": ("BH", "BAH"), "amman": ("JO", "AMM"), "beirut": ("LB", "BEY"),
    "tel aviv": ("IL", "TLV"), "tehran": ("IR", "IKA"),
    # ── South & Central Asia ──
    "kathmandu": ("NP", "KTM"), "pokhara": ("NP", "PKR"), "colombo": ("LK", "CMB"),
    "male": ("MV", "MLE"), "maldives": ("MV", "MLE"), "dhaka": ("BD", "DAC"),
    "thimphu": ("BT", "PBH"), "paro": ("BT", "PBH"), "baku": ("AZ", "GYD"),
    "tashkent": ("UZ", "TAS"), "samarkand": ("UZ", "SKD"), "almaty": ("KZ", "ALA"),
    "astana": ("KZ", "NQZ"), "bishkek": ("KG", "FRU"), "yerevan": ("AM", "EVN"),
    "tbilisi": ("GE", "TBS"),
    # ── Southeast Asia ──
    "singapore": ("SG", "SIN"), "bangkok": ("TH", "BKK"), "phuket": ("TH", "HKT"),
    "pattaya": ("TH", "BKK"), "krabi": ("TH", "KBV"), "chiang mai": ("TH", "CNX"),
    "koh samui": ("TH", "USM"), "kuala lumpur": ("MY", "KUL"), "langkawi": ("MY", "LGK"),
    "penang": ("MY", "PEN"), "bali": ("ID", "DPS"), "jakarta": ("ID", "CGK"),
    "surabaya": ("ID", "SUB"), "manila": ("PH", "MNL"), "cebu": ("PH", "CEB"),
    "hanoi": ("VN", "HAN"), "ho chi minh": ("VN", "SGN"), "da nang": ("VN", "DAD"),
    "phnom penh": ("KH", "PNH"), "siem reap": ("KH", "REP"), "vientiane": ("LA", "VTE"),
    "yangon": ("MM", "RGN"),
    # ── East Asia ──
    "tokyo": ("JP", "NRT"), "osaka": ("JP", "KIX"), "nagoya": ("JP", "NGO"),
    "fukuoka": ("JP", "FUK"), "sapporo": ("JP", "CTS"), "seoul": ("KR", "ICN"),
    "busan": ("KR", "PUS"), "beijing": ("CN", "PEK"), "shanghai": ("CN", "PVG"),
    "guangzhou": ("CN", "CAN"), "shenzhen": ("CN", "SZX"), "chengdu": ("CN", "CTU"),
    "hong kong": ("HK", "HKG"), "macau": ("MO", "MFM"), "taipei": ("TW", "TPE"),
    "ulaanbaatar": ("MN", "UBN"),
    # ── Europe ──
    "london": ("GB", "LHR"), "manchester": ("GB", "MAN"), "edinburgh": ("GB", "EDI"),
    "glasgow": ("GB", "GLA"), "birmingham": ("GB", "BHX"), "paris": ("FR", "CDG"),
    "nice": ("FR", "NCE"), "lyon": ("FR", "LYS"), "marseille": ("FR", "MRS"),
    "frankfurt": ("DE", "FRA"), "munich": ("DE", "MUC"), "berlin": ("DE", "BER"),
    "hamburg": ("DE", "HAM"), "dusseldorf": ("DE", "DUS"), "stuttgart": ("DE", "STR"),
    "koln": ("DE", "CGN"), "amsterdam": ("NL", "AMS"), "rome": ("IT", "FCO"),
    "milan": ("IT", "MXP"), "venice": ("IT", "VCE"), "naples": ("IT", "NAP"),
    "florence": ("IT", "FLR"), "bologna": ("IT", "BLQ"), "madrid": ("ES", "MAD"),
    "barcelona": ("ES", "BCN"), "malaga": ("ES", "AGP"), "zurich": ("CH", "ZRH"),
    "geneva": ("CH", "GVA"), "vienna": ("AT", "VIE"), "dublin": ("IE", "DUB"),
    "lisbon": ("PT", "LIS"), "porto": ("PT", "OPO"), "brussels": ("BE", "BRU"),
    "copenhagen": ("DK", "CPH"), "stockholm": ("SE", "ARN"), "oslo": ("NO", "OSL"),
    "helsinki": ("FI", "HEL"), "reykjavik": ("IS", "KEF"), "athens": ("GR", "ATH"),
    "santorini": ("GR", "JTR"), "prague": ("CZ", "PRG"), "budapest": ("HU", "BUD"),
    "warsaw": ("PL", "WAW"), "krakow": ("PL", "KRK"), "bucharest": ("RO", "OTP"),
    "sofia": ("BG", "SOF"), "belgrade": ("RS", "BEG"), "zagreb": ("HR", "ZAG"),
    "ljubljana": ("SI", "LJU"), "tallinn": ("EE", "TLL"), "riga": ("LV", "RIX"),
    "vilnius": ("LT", "VNO"), "valletta": ("MT", "MLA"), "moscow": ("RU", "SVO"),
    "saint petersburg": ("RU", "LED"), "kyiv": ("UA", "KBP"), "istanbul": ("TR", "IST"),
    "antalya": ("TR", "AYT"),
    # ── North America ──
    "new york": ("US", "JFK"), "los angeles": ("US", "LAX"), "san francisco": ("US", "SFO"),
    "chicago": ("US", "ORD"), "washington": ("US", "IAD"), "boston": ("US", "BOS"),
    "seattle": ("US", "SEA"), "dallas": ("US", "DFW"), "houston": ("US", "IAH"),
    "atlanta": ("US", "ATL"), "miami": ("US", "MIA"), "las vegas": ("US", "LAS"),
    "denver": ("US", "DEN"), "phoenix": ("US", "PHX"), "philadelphia": ("US", "PHL"),
    "orlando": ("US", "MCO"), "detroit": ("US", "DTW"), "minneapolis": ("US", "MSP"),
    "san diego": ("US", "SAN"), "austin": ("US", "AUS"), "portland": ("US", "PDX"),
    "honolulu": ("US", "HNL"), "toronto": ("CA", "YYZ"), "vancouver": ("CA", "YVR"),
    "montreal": ("CA", "YUL"), "calgary": ("CA", "YYC"), "mexico city": ("MX", "MEX"),
    "cancun": ("MX", "CUN"), "panama city": ("PA", "PTY"), "havana": ("CU", "HAV"),
    "san jose": ("CR", "SJO"),
    # ── South America ──
    "sao paulo": ("BR", "GRU"), "rio de janeiro": ("BR", "GIG"), "buenos aires": ("AR", "EZE"),
    "lima": ("PE", "LIM"), "bogota": ("CO", "BOG"), "santiago": ("CL", "SCL"),
    "quito": ("EC", "UIO"),
    # ── Africa ──
    "cairo": ("EG", "CAI"), "nairobi": ("KE", "NBO"), "johannesburg": ("ZA", "JNB"),
    "cape town": ("ZA", "CPT"), "addis ababa": ("ET", "ADD"), "lagos": ("NG", "LOS"),
    "accra": ("GH", "ACC"), "casablanca": ("MA", "CMN"), "marrakech": ("MA", "RAK"),
    "dar es salaam": ("TZ", "DAR"), "zanzibar": ("TZ", "ZNZ"), "mauritius": ("MU", "MRU"),
    "seychelles": ("SC", "SEZ"),
    # ── Oceania ──
    "sydney": ("AU", "SYD"), "melbourne": ("AU", "MEL"), "brisbane": ("AU", "BNE"),
    "perth": ("AU", "PER"), "adelaide": ("AU", "ADL"), "gold coast": ("AU", "OOL"),
    "auckland": ("NZ", "AKL"), "christchurch": ("NZ", "CHC"), "queenstown": ("NZ", "ZQN"),
    "wellington": ("NZ", "WLG"), "nadi": ("FJ", "NAN"),
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
