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
import unicodedata
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
    # region / landmark phrasings → nearest covered gateway city
    "hoi an": "da nang",
    "cabo san lucas": "los cabos",
    "amalfi": "naples",
    "amalfi coast": "naples",
    "positano": "naples",
    "tuscany": "florence",
    "lake como": "milan",
    "costa del sol": "malaga",
    "french riviera": "nice",
    "bavaria": "munich",
    "scottish highlands": "edinburgh",
    "swiss alps": "zurich",
    "phuket island": "phuket",
    "angkor wat": "siem reap",
    "halong bay": "hanoi",
    "great barrier reef": "cairns",
    "niagara falls": "toronto",
    "grand canyon": "phoenix",
}


def normalize(city: str) -> str:
    """
    Lowercase, fold accents (Zürich→zurich, Malé→male), strip punctuation,
    collapse whitespace, resolve aliases. Accent folding matters: without it the
    punctuation strip deletes the bare accented letter (ü→'') giving 'zrich'.
    """
    s = (city or "").lower().strip()
    # NFKD splits 'ü' → 'u' + combining diaeresis; drop the combining marks.
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return _ALIASES.get(s, s)


def _match_key(city: str) -> str:
    """
    Resolve messy city strings to a known table key. Gemini (and OTA autocompletes)
    emit names like "Delhi Airport", "Tokyo Narita", "Dubai International Airport",
    "New Delhi" — none of which are bare keys. We try the full normalised string,
    then every contiguous word-span (longest first), accepting only spans that are
    real table keys. This strips airport/terminal/qualifier noise generically
    without a hand-maintained stopword list, and never invents a city: "tokyo
    narita"→"tokyo", "delhi airport"→"delhi", "new delhi"→"delhi", "los angeles
    intl airport"→"los angeles". Unknown strings pass through unchanged.
    """
    s = normalize(city)
    if s in _INDIA or s in _FOREIGN:
        return s
    toks = s.split()
    n = len(toks)
    if n > 1:
        for length in range(n, 0, -1):          # longest span wins
            for start in range(n - length + 1):
                span = _ALIASES.get(" ".join(toks[start:start + length]), " ".join(toks[start:start + length]))
                if span in _INDIA or span in _FOREIGN:
                    return span
    return s


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
    # ── more commercial airports (own IATA) ──
    "hindon": "HDO", "ludhiana": "LUH", "jalandhar": "AIP", "bathinda": "BUP",
    "pathankot": "IXP", "hisar": "HSS", "shivamogga": "RQY", "shimoga": "RQY",
    "jalgaon": "JLG", "nanded": "NDC", "gondia": "GDB", "akola": "AKD",
    "porbandar": "PBD", "bhavnagar": "BHU", "kandla": "IXY", "keshod": "IXK",
    "kalaburagi": "GBI", "gulbarga": "GBI", "ballari": "VDY", "bellary": "VDY",
    "jeypore": "PYB", "rourkela": "RRK", "cooch behar": "COH", "lilabari": "IXI",
    "north lakhimpur": "IXI", "rupsi": "RUP", "tezu": "TEI", "rewa": "REW",
    "bilaspur": "PAB", "nellore": "TIR", "puttaparthi": "PUT",
    # ── more tourist towns → nearest serviceable airport ──
    "kovalam": "TRV", "kumarakom": "COK", "thekkady": "COK", "vagamon": "COK",
    "bekal": "CNN", "kasaragod": "CNN", "thrissur": "COK", "kollam": "TRV",
    "murudeshwar": "IXE", "udupi": "IXE", "karwar": "IXE", "dandeli": "HBX",
    "jog falls": "HBX", "badami": "HBX", "bijapur": "GBI", "vijayapura": "GBI",
    "belur": "HBX", "halebidu": "HBX", "nandi hills": "BLR", "chikballapur": "BLR",
    "coonoor": "CJB", "valparai": "CJB", "masinagudi": "CJB", "yelagiri": "SXV",
    "thanjavur": "TRZ", "kumbakonam": "TRZ", "velankanni": "TRZ", "chidambaram": "PNY",
    "kanchipuram": "MAA", "vellore": "MAA", "hogenakkal": "SXV", "araku": "VTZ",
    "araku valley": "VTZ", "lambasingi": "VTZ", "srikalahasti": "TIR",
    "horsley hills": "TIR", "gandikota": "CDP", "srisailam": "HYD", "warangal": "HYD",
    "ajanta": "IXU", "ellora": "IXU", "lonar": "IXU", "bhandardara": "ISK",
    "alibaug": "BOM", "kashid": "BOM", "tarkarli": "RTC", "ganpatipule": "KLH",
    "daman": "STV", "kumbhalgarh": "UDR", "ranakpur": "UDR", "nathdwara": "UDR",
    "mandawa": "JAI", "shekhawati": "JAI", "alwar": "JAI", "neemrana": "JAI",
    "barmer": "JSA", "osian": "JDH", "khimsar": "JDH", "junagadh": "RAJ",
    "palitana": "BHU", "statue of unity": "BDQ", "kevadia": "BDQ", "modhera": "AMD",
    "patan": "AMD", "dholavira": "BHJ", "champaner": "BDQ", "pavagadh": "BDQ",
    "orchha": "GWL", "chitrakoot": "REW", "panna": "HJR", "pachmarhi": "BHO",
    "kanha": "JLR", "bandhavgarh": "JLR", "pench": "NAG", "tadoba": "NAG",
    "anandpur sahib": "IXC", "spiti valley": "KUU", "kaza": "KUU", "kalpa": "KUU",
    "narkanda": "SLV", "barot": "DHM", "morni": "IXC", "pinjore": "IXC",
    "joshimath": "DED", "munsiyari": "PGH", "pithoragarh": "PGH",
    "valley of flowers": "DED", "hemkund sahib": "DED", "champawat": "PGH",
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
    "papeete": ("PF", "PPT"), "tahiti": ("PF", "PPT"), "bora bora": ("PF", "BOB"),
    "french polynesia": ("PF", "PPT"),
    # ── Micro-states & enclaves → nearest serviceable gateway (no own airport) ──
    "liechtenstein": ("LI", "ZRH"), "vaduz": ("LI", "ZRH"),
    "monaco": ("MC", "NCE"), "monte carlo": ("MC", "NCE"),
    "andorra": ("AD", "BCN"), "andorra la vella": ("AD", "BCN"),
    "vatican city": ("VA", "FCO"), "vatican": ("VA", "FCO"),
    "san marino": ("SM", "RMI"),
    "luxembourg": ("LU", "LUX"), "luxembourg city": ("LU", "LUX"),
    "malta": ("MT", "MLA"),  # "valletta" already mapped in the Europe block
    # ── Remote islands / arctic / landmark gateways ──
    "iceland": ("IS", "KEF"), "keflavik": ("IS", "KEF"),
    "greenland": ("GL", "GOH"), "nuuk": ("GL", "GOH"), "kangerlussuaq": ("GL", "SFJ"),
    "faroe islands": ("FO", "FAE"), "torshavn": ("FO", "FAE"), "vagar": ("FO", "FAE"),
    "svalbard": ("NO", "LYR"), "longyearbyen": ("NO", "LYR"),
    "easter island": ("CL", "IPC"), "hanga roa": ("CL", "IPC"), "mataveri": ("CL", "IPC"),
    "serengeti": ("TZ", "JRO"), "arusha": ("TZ", "JRO"), "kilimanjaro": ("TZ", "JRO"),
    "yellowstone": ("US", "BZN"), "bozeman": ("US", "BZN"),
    "jackson hole": ("US", "JAC"), "jackson": ("US", "JAC"),
    # ── Countries sometimes passed as the destination itself ──
    "ireland": ("IE", "DUB"), "scotland": ("GB", "EDI"),
    # ── Europe (secondary cities + holiday gateways) ──
    "valencia": ("ES", "VLC"), "seville": ("ES", "SVQ"), "bilbao": ("ES", "BIO"),
    "palma": ("ES", "PMI"), "mallorca": ("ES", "PMI"), "ibiza": ("ES", "IBZ"),
    "alicante": ("ES", "ALC"), "granada": ("ES", "GRX"), "santiago de compostela": ("ES", "SCQ"),
    "faro": ("PT", "FAO"), "madeira": ("PT", "FNC"), "funchal": ("PT", "FNC"), "azores": ("PT", "PDL"),
    "turin": ("IT", "TRN"), "pisa": ("IT", "PSA"), "catania": ("IT", "CTA"), "palermo": ("IT", "PMO"),
    "bari": ("IT", "BRI"), "cagliari": ("IT", "CAG"), "verona": ("IT", "VRN"), "genoa": ("IT", "GOA"),
    "basel": ("CH", "BSL"), "salzburg": ("AT", "SZG"), "innsbruck": ("AT", "INN"), "graz": ("AT", "GRZ"),
    "nuremberg": ("DE", "NUE"), "hannover": ("DE", "HAJ"), "leipzig": ("DE", "LEJ"), "dresden": ("DE", "DRS"),
    "bremen": ("DE", "BRE"), "rotterdam": ("NL", "RTM"), "eindhoven": ("NL", "EIN"),
    "gothenburg": ("SE", "GOT"), "malmo": ("SE", "MMX"), "bergen": ("NO", "BGO"), "stavanger": ("NO", "SVG"),
    "trondheim": ("NO", "TRD"), "tromso": ("NO", "TOS"), "aarhus": ("DK", "AAR"), "billund": ("DK", "BLL"),
    "gdansk": ("PL", "GDN"), "wroclaw": ("PL", "WRO"), "poznan": ("PL", "POZ"), "katowice": ("PL", "KTW"),
    "bratislava": ("SK", "BTS"), "cluj": ("RO", "CLJ"), "timisoara": ("RO", "TSR"),
    "varna": ("BG", "VAR"), "burgas": ("BG", "BOJ"), "thessaloniki": ("GR", "SKG"), "heraklion": ("GR", "HER"),
    "crete": ("GR", "HER"), "rhodes": ("GR", "RHO"), "corfu": ("GR", "CFU"), "mykonos": ("GR", "JMK"),
    "kos": ("GR", "KGS"), "split": ("HR", "SPU"), "dubrovnik": ("HR", "DBV"), "zadar": ("HR", "ZAD"),
    "sarajevo": ("BA", "SJJ"), "skopje": ("MK", "SKP"), "tirana": ("AL", "TIA"), "podgorica": ("ME", "TGD"),
    "kotor": ("ME", "TIV"), "tivat": ("ME", "TIV"), "chisinau": ("MD", "KIV"), "lviv": ("UA", "LWO"),
    "batumi": ("GE", "BUS"), "kutaisi": ("GE", "KUT"),
    # ── Turkey & Middle East (more) ──
    "ankara": ("TR", "ESB"), "izmir": ("TR", "ADB"), "bodrum": ("TR", "BJV"), "dalaman": ("TR", "DLM"),
    "cappadocia": ("TR", "NAV"), "nevsehir": ("TR", "NAV"), "kayseri": ("TR", "ASR"), "trabzon": ("TR", "TZX"),
    "aqaba": ("JO", "AQJ"), "shiraz": ("IR", "SYZ"), "isfahan": ("IR", "IFN"), "mashhad": ("IR", "MHD"),
    "medina": ("SA", "MED"), "najaf": ("IQ", "NJF"), "erbil": ("IQ", "EBL"), "baghdad": ("IQ", "BGW"),
    "sharm el sheikh": ("EG", "SSH"), "hurghada": ("EG", "HRG"), "luxor": ("EG", "LXR"), "aswan": ("EG", "ASW"),
    # ── Asia (more) ──
    "chiang rai": ("TH", "CEI"), "hat yai": ("TH", "HDY"), "surat thani": ("TH", "URT"),
    "luang prabang": ("LA", "LPQ"), "mandalay": ("MM", "MDL"), "kuching": ("MY", "KCH"),
    "kota kinabalu": ("MY", "BKI"), "johor bahru": ("MY", "JHB"), "ipoh": ("MY", "IPH"),
    "bandung": ("ID", "BDO"), "yogyakarta": ("ID", "YIA"), "lombok": ("ID", "LOP"), "medan": ("ID", "KNO"),
    "makassar": ("ID", "UPG"), "davao": ("PH", "DVO"), "puerto princesa": ("PH", "PPS"),
    "boracay": ("PH", "MPH"), "caticlan": ("PH", "MPH"), "kalibo": ("PH", "KLO"),
    "da lat": ("VN", "DLI"), "nha trang": ("VN", "CXR"), "phu quoc": ("VN", "PQC"), "hue": ("VN", "HUI"),
    "haiphong": ("VN", "HPH"),
    # ── East Asia (more) ──
    "naha": ("JP", "OKA"), "okinawa": ("JP", "OKA"), "hiroshima": ("JP", "HIJ"), "sendai": ("JP", "SDJ"),
    "kagoshima": ("JP", "KOJ"), "kobe": ("JP", "UKB"), "jeju": ("KR", "CJU"), "daegu": ("KR", "TAE"),
    "xian": ("CN", "XIY"), "hangzhou": ("CN", "HGH"), "nanjing": ("CN", "NKG"), "qingdao": ("CN", "TAO"),
    "kunming": ("CN", "KMG"), "xiamen": ("CN", "XMN"), "wuhan": ("CN", "WUH"), "sanya": ("CN", "SYX"),
    "guilin": ("CN", "KWL"), "chongqing": ("CN", "CKG"), "harbin": ("CN", "HRB"), "dalian": ("CN", "DLC"),
    "kaohsiung": ("TW", "KHH"),
    # ── Oceania (more) ──
    "cairns": ("AU", "CNS"), "darwin": ("AU", "DRW"), "hobart": ("AU", "HBA"), "canberra": ("AU", "CBR"),
    "sunshine coast": ("AU", "MCY"), "alice springs": ("AU", "ASP"), "townsville": ("AU", "TSV"),
    "dunedin": ("NZ", "DUD"), "rotorua": ("NZ", "ROT"), "noumea": ("NC", "NOU"), "port vila": ("VU", "VLI"),
    "apia": ("WS", "APW"), "nukualofa": ("TO", "TBU"), "rarotonga": ("CK", "RAR"), "guam": ("GU", "GUM"),
    # ── North America (more) ──
    "nashville": ("US", "BNA"), "new orleans": ("US", "MSY"), "salt lake city": ("US", "SLC"),
    "kansas city": ("US", "MCI"), "st louis": ("US", "STL"), "raleigh": ("US", "RDU"), "charlotte": ("US", "CLT"),
    "pittsburgh": ("US", "PIT"), "cleveland": ("US", "CLE"), "cincinnati": ("US", "CVG"),
    "columbus": ("US", "CMH"), "indianapolis": ("US", "IND"), "milwaukee": ("US", "MKE"),
    "sacramento": ("US", "SMF"), "san jose california": ("US", "SJC"), "oakland": ("US", "OAK"),
    "tampa": ("US", "TPA"), "fort lauderdale": ("US", "FLL"), "anchorage": ("US", "ANC"),
    "albuquerque": ("US", "ABQ"), "tucson": ("US", "TUS"), "memphis": ("US", "MEM"),
    "jacksonville": ("US", "JAX"), "palm springs": ("US", "PSP"), "savannah": ("US", "SAV"),
    "charleston": ("US", "CHS"), "asheville": ("US", "AVL"),
    "ottawa": ("CA", "YOW"), "edmonton": ("CA", "YEG"), "winnipeg": ("CA", "YWG"), "halifax": ("CA", "YHZ"),
    "quebec city": ("CA", "YQB"), "victoria": ("CA", "YYJ"), "kelowna": ("CA", "YLW"),
    "guadalajara": ("MX", "GDL"), "monterrey": ("MX", "MTY"), "tijuana": ("MX", "TIJ"),
    "los cabos": ("MX", "SJD"), "cabo": ("MX", "SJD"), "puerto vallarta": ("MX", "PVR"), "merida": ("MX", "MID"),
    "tulum": ("MX", "CUN"), "nassau": ("BS", "NAS"), "montego bay": ("JM", "MBJ"), "kingston": ("JM", "KIN"),
    "punta cana": ("DO", "PUJ"), "santo domingo": ("DO", "SDQ"), "san juan": ("PR", "SJU"),
    "aruba": ("AW", "AUA"), "barbados": ("BB", "BGI"), "st lucia": ("LC", "UVF"), "grand cayman": ("KY", "GCM"),
    "belize city": ("BZ", "BZE"), "guatemala city": ("GT", "GUA"), "san salvador": ("SV", "SAL"),
    # ── South America (more) ──
    "medellin": ("CO", "MDE"), "cartagena": ("CO", "CTG"), "cusco": ("PE", "CUZ"), "arequipa": ("PE", "AQP"),
    "la paz": ("BO", "LPB"), "santa cruz": ("BO", "VVI"), "montevideo": ("UY", "MVD"), "asuncion": ("PY", "ASU"),
    "guayaquil": ("EC", "GYE"), "galapagos": ("EC", "GPS"), "manaus": ("BR", "MAO"), "brasilia": ("BR", "BSB"),
    "salvador": ("BR", "SSA"), "recife": ("BR", "REC"), "fortaleza": ("BR", "FOR"), "florianopolis": ("BR", "FLN"),
    "porto alegre": ("BR", "POA"), "mendoza": ("AR", "MDZ"), "bariloche": ("AR", "BRC"), "ushuaia": ("AR", "USH"),
    "iguazu falls": ("AR", "IGR"), "foz do iguacu": ("BR", "IGU"),
    # ── Africa (more) ──
    "abuja": ("NG", "ABV"), "mombasa": ("KE", "MBA"), "kigali": ("RW", "KGL"), "entebbe": ("UG", "EBB"),
    "kampala": ("UG", "EBB"), "victoria falls": ("ZW", "VFA"), "windhoek": ("NA", "WDH"),
    "gaborone": ("BW", "GBE"), "maputo": ("MZ", "MPM"), "antananarivo": ("MG", "TNR"),
    "durban": ("ZA", "DUR"), "lusaka": ("ZM", "LUN"), "harare": ("ZW", "HRE"), "dakar": ("SN", "DSS"),
    "abidjan": ("CI", "ABJ"), "tunis": ("TN", "TUN"), "algiers": ("DZ", "ALG"), "agadir": ("MA", "AGA"),
    "fes": ("MA", "FEZ"), "tangier": ("MA", "TNG"), "khartoum": ("SD", "KRT"), "djibouti": ("DJ", "JIB"),
    "reunion": ("RE", "RUN"),
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
    key = _match_key(city)
    if key in _INDIA:
        return "IN"
    f = _FOREIGN.get(key)
    return f[0] if f else None


def iata_of(city: str) -> Optional[str]:
    """Primary IATA airport code for a city, or None if unknown / no airport."""
    key = _match_key(city)
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


# ─── Autocomplete (offline location search) ──────────────────────────────────
# Powers /autocomplete/airports + /autocomplete/destinations. Pure in-memory
# substring search over the same tables the search pipeline already trusts —
# zero network, zero anti-bot risk, instant. The live OTA autosuggests stay the
# long-tail fallback (see app.services.autocomplete).

# Minimal ISO-3166 alpha-2 → display name, only for the countries our tables
# actually contain. Unknown codes fall back to the bare code in the label.
_COUNTRY_NAME: dict[str, str] = {
    "IN": "India", "AE": "UAE", "QA": "Qatar", "OM": "Oman", "SA": "Saudi Arabia",
    "KW": "Kuwait", "BH": "Bahrain", "JO": "Jordan", "LB": "Lebanon", "IL": "Israel",
    "IR": "Iran", "NP": "Nepal", "LK": "Sri Lanka", "MV": "Maldives", "BD": "Bangladesh",
    "BT": "Bhutan", "AZ": "Azerbaijan", "UZ": "Uzbekistan", "KZ": "Kazakhstan",
    "KG": "Kyrgyzstan", "AM": "Armenia", "GE": "Georgia", "SG": "Singapore",
    "TH": "Thailand", "MY": "Malaysia", "ID": "Indonesia", "VN": "Vietnam",
    "PH": "Philippines", "KH": "Cambodia", "LA": "Laos", "MM": "Myanmar",
    "JP": "Japan", "KR": "South Korea", "CN": "China", "HK": "Hong Kong",
    "TW": "Taiwan", "MO": "Macau", "MN": "Mongolia", "AU": "Australia",
    "NZ": "New Zealand", "FJ": "Fiji", "PF": "French Polynesia", "GB": "United Kingdom",
    "FR": "France", "DE": "Germany", "IT": "Italy", "ES": "Spain", "PT": "Portugal",
    "NL": "Netherlands", "BE": "Belgium", "CH": "Switzerland", "AT": "Austria",
    "GR": "Greece", "TR": "Turkey", "RU": "Russia", "IE": "Ireland", "IS": "Iceland",
    "NO": "Norway", "SE": "Sweden", "DK": "Denmark", "FI": "Finland", "PL": "Poland",
    "CZ": "Czechia", "HU": "Hungary", "HR": "Croatia", "RO": "Romania", "US": "USA",
    "CA": "Canada", "MX": "Mexico", "BR": "Brazil", "AR": "Argentina", "PE": "Peru",
    "CL": "Chile", "CO": "Colombia", "ZA": "South Africa", "EG": "Egypt",
    "KE": "Kenya", "TZ": "Tanzania", "MA": "Morocco", "MU": "Mauritius",
    "SC": "Seychelles", "ET": "Ethiopia", "LI": "Liechtenstein", "MC": "Monaco",
    "AD": "Andorra", "VA": "Vatican", "LU": "Luxembourg", "GL": "Greenland",
    "FO": "Faroe Islands",
}

# canonical key → its aliases, so typing "bombay"/"bom" surfaces Mumbai even
# though normalize() only rewrites a full-string alias.
_ALIAS_BY_CANON: dict[str, list[str]] = {}
for _alias, _canon in _ALIASES.items():
    _ALIAS_BY_CANON.setdefault(_canon, []).append(_alias)


def country_name(code: Optional[str]) -> Optional[str]:
    """Display country name for an ISO alpha-2 code, falling back to the code."""
    if not code:
        return None
    return _COUNTRY_NAME.get(code.upper(), code.upper())


def _iter_locations():
    """Yield (key, country_code, iata) for every city in both tables."""
    for key, code in _INDIA.items():
        yield key, "IN", code
    for key, (cc, code) in _FOREIGN.items():
        yield key, cc, code


def _match_score(nq: str, names: list[str]) -> float:
    """
    Rank a query against a city's names (canonical + aliases). Exact > prefix >
    word-boundary > substring. 0 means no match. Shorter, closer names win ties.
    """
    best = 0.0
    for name in names:
        if not name:
            continue
        if name == nq:
            best = max(best, 100.0)
        elif name.startswith(nq):
            # Closer-length prefixes rank higher (e.g. "goa" beats "goalpara").
            best = max(best, 70.0 - min(len(name) - len(nq), 20))
        elif f" {nq}" in f" {name}":          # matches start of any word
            best = max(best, 45.0)
        elif nq in name:
            best = max(best, 25.0)
    return best


def search_locations(q: str, limit: int = 8, require_iata: bool = False) -> list[dict]:
    """
    Offline autocomplete over the geo tables. Returns ranked
    ``[{city, country_code, country, iata}]``. When ``require_iata`` is set
    (flight origins/destinations) cities without a servicing airport are skipped.
    """
    nq = normalize(q)
    if not nq:
        return []

    scored: list[tuple[float, str, str, Optional[str]]] = []
    for key, cc, code in _iter_locations():
        if require_iata and not code:
            continue
        names = [key] + _ALIAS_BY_CANON.get(key, [])
        score = _match_score(nq, names)
        if score <= 0:
            continue
        scored.append((score, key, cc, code))

    # Highest score first; tie-break by shorter (more canonical) name, then alpha.
    scored.sort(key=lambda r: (-r[0], len(r[1]), r[1]))

    out: list[dict] = []
    for _score, key, cc, code in scored[:limit]:
        out.append({
            "city": key.title(),
            "country_code": cc,
            "country": country_name(cc),
            "iata": code,
        })
    return out
