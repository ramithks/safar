"""
Indian Railway search via ConfirmTKT API.
India-only — only applies when both origin and destination are Indian cities.
"""
from datetime import datetime
import httpx

_BASE = "https://cttrainsapi.confirmtkt.com"
_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "ApiKey": "ct-web!2$",   # ct-web!2$
    "CT-Token": "",
    "CT-Userkey": "",
    "ClientId": "ct-web",
    "Content-Type": "application/json",
    "DeviceId": "7558c77e-71eb-423f-9575-5c3c9d399a19",
    "Origin": "https://www.confirmtkt.com",
    "Referer": "https://www.confirmtkt.com/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}

# city/town name (lowercase) → Indian Railway station code
STATION_CODE_MAP: dict[str, str | None] = {
    # Major metros
    "new delhi": "NDLS", "delhi": "NDLS", "old delhi": "DLI",
    "mumbai": "CSTM", "mumbai csmt": "CSTM", "mumbai central": "BCT", "bandra terminus": "BDTS",
    "kolkata": "HWH", "howrah": "HWH", "sealdah": "SDAH",
    "bangalore": "SBC", "bengaluru": "SBC", "ksr bengaluru": "SBC",
    "chennai": "MAS", "chennai central": "MAS", "chennai egmore": "MS",
    "hyderabad": "SC", "secunderabad": "SC", "hyderabad deccan": "HYB",
    # Tier-1 cities
    "ahmedabad": "ADI", "pune": "PUNE", "jaipur": "JP",
    "lucknow": "LKO", "charbagh": "LKO",
    "bhopal": "BPL", "nagpur": "NGP", "patna": "PNBE",
    "chandigarh": "CDG", "amritsar": "ASR", "ludhiana": "LDH",
    "agra cantt": "AGC", "agra fort": "AF", "agra": "AGC",
    "varanasi": "BSB", "bhubaneswar": "BBS",
    "visakhapatnam": "VSKP", "vizag": "VSKP",
    "kochi": "ERS", "ernakulam": "ERS",
    "trivandrum": "TVC", "thiruvananthapuram": "TVC",
    "coimbatore": "CBE", "madurai": "MDU",
    "indore": "INDB", "surat": "ST", "vadodara": "BRC", "baroda": "BRC",
    "rajkot": "RJT", "raipur": "R", "bilaspur": "BSP",
    "guwahati": "GHY",
    # North India
    "dehradun": "DDN", "haridwar": "HW", "rishikesh": "RKSH",
    "roorkee": "RK", "saharanpur": "SRE",
    "meerut": "MTC", "aligarh": "ALJN",
    "moradabad": "MB", "bareilly": "BE",
    "kanpur": "CNB", "lucknow": "LKO",
    "gorakhpur": "GKP", "allahabad": "PRYJ", "prayagraj": "PRYJ",
    "gaya": "GAYA", "ranchi": "RNC",
    "jamshedpur": "TATA", "tatanagar": "TATA", "dhanbad": "DHN",
    "asansol": "ASN", "durgapur": "DGR", "kharagpur": "KGP",
    "new jalpaiguri": "NJP", "siliguri": "NJP", "bagdogra": "NJP",
    # Rajasthan
    "jodhpur": "JU", "udaipur": "UDZ", "ajmer": "AII", "kota": "KOTA",
    "bikaner": "BKN", "alwar": "AWR", "bharatpur": "BTE", "jaisalmer": "JSM",
    "sawai madhopur": "SWM",
    # Punjab / Haryana
    "pathankot": "PTHK", "jammu": "JAT", "jammu tawi": "JAT",
    "bathinda": "BTI", "ferozepur": "FZR", "ambala": "UMB",
    # Himachal (limited rail)
    "shimla": "SML", "kalka": "KLK", "solan": "SOL",
    "kasauli": "KLK",  # nearest
    "dharamshala": "PTHK", "mcleod ganj": "PTHK", "dalhousie": "PTHK",
    # Hill stations / remote — no rail (return None so caller knows)
    "manali": None, "kullu": None, "aut": None, "bhuntar": None, "kasol": None,
    "kheerganga": None, "spiti": None, "lahaul": None, "pin valley": None,
    "leh": None, "ladakh": None, "srinagar": None,
    "darjeeling": "NJP", "gangtok": "NJP", "kalimpong": "NJP",
    "shillong": "GHY",
    # Central India
    "jabalpur": "JBP", "katni": "KTE", "ujjain": "UJN", "indore": "INDB",
    "gwalior": "GWL", "jhansi": "JHS", "ratlam": "RTM",
    # South India
    "mysore": "MYS", "mysuru": "MYS",
    "mangalore": "MAQ", "mangaluru": "MAQ",
    "hubli": "UBL", "hubballi": "UBL", "belgaum": "BGM", "belagavi": "BGM",
    "vijayawada": "BZA", "tirupati": "TPTY", "guntur": "GNT",
    "rajahmundry": "RJY", "nellore": "NLR", "warangal": "WL",
    "tiruchirappalli": "TPJ", "trichy": "TPJ",
    "thanjavur": "TJ", "salem": "SA", "erode": "ED", "tirunelveli": "TEN",
    "coimbatore": "CBE", "vellore": "KPD",
    "pondicherry": "PDY", "puducherry": "PDY",
    # Goa
    "goa": "MAO", "madgaon": "MAO", "margao": "MAO", "panaji": "MAO", "vasco": "VSG",
    # Maharashtra
    "aurangabad": "AWB", "nashik": "NK", "nashik road": "NK",
    "kolhapur": "KOP", "solapur": "SUR",
    # North-East
    "dibrugarh": "DBRG", "jorhat": "JTTN",
    "agartala": "AGTL", "imphal": None, "aizawl": None, "shillong": "GHY",
}


def city_to_station_code(city_name: str) -> str | None:
    """
    Map a city/place name to an Indian Railway station code.
    Returns None if no rail connectivity (remote hill areas) or unknown.
    """
    key = city_name.lower().strip()
    if key in STATION_CODE_MAP:
        return STATION_CODE_MAP[key]
    for name, code in STATION_CODE_MAP.items():
        if name and (name in key or key in name):
            return code
    return None


async def search_trains(
    source_code: str,
    dest_code: str,
    date: str,           # "YYYY-MM-DD" or "DD-MM-YYYY"
    limit: int = 5,
) -> list[dict]:
    """
    Search trains between two station codes via ConfirmTKT.
    Returns a normalized list of train options.
    """
    try:
        parts = date.split("-")
        if len(parts) == 3 and len(parts[0]) == 4:
            doj = datetime.strptime(date, "%Y-%m-%d").strftime("%d-%m-%Y")
        else:
            doj = date
    except Exception:
        doj = date

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(
                f"{_BASE}/api/v1/trains/search",
                headers=_HEADERS,
                params={
                    "sourceStationCode": source_code,
                    "destinationStationCode": dest_code,
                    "addAvailabilityCache": "true",
                    "excludeMultiTicketAlternates": "false",
                    "excludeBoostAlternates": "false",
                    "sortBy": "DEFAULT",
                    "dateOfJourney": doj,
                    "enableNearby": "true",
                    "enableTG": "true",
                    "tGPlan": "CTG-A42",
                    "showTGPrediction": "false",
                    "tgColor": "DEFAULT",
                    "showPredictionGlobal": "true",
                    "showNewAlternates": "false",
                    "showNewAltText": "true",
                },
            )
        r.raise_for_status()
        data = r.json()
        trains_raw = (
            data.get("trainsList")
            or data.get("trains")
            or data.get("TrainList")
            or []
        )
        return [_normalise_train(t) for t in trains_raw[:limit]]
    except Exception as e:
        print(f"[railway] search {source_code}→{dest_code} on {doj}: {e}")
        return []


def _normalise_train(t: dict) -> dict:
    classes_raw = (
        t.get("classesWithFare")
        or t.get("availableClasses")
        or t.get("classes")
        or []
    )
    cheapest_fare: int | None = None
    cheapest_class: str | None = None
    available_classes = []
    for c in classes_raw:
        code = c.get("classCode") or c.get("code") or c.get("className")
        fare = c.get("fare") or c.get("totalFare") or c.get("price")
        seats = c.get("availableSeats") or c.get("available") or c.get("seatsAvailable")
        available_classes.append({"code": code, "fare": fare, "available_seats": seats})
        if fare and (cheapest_fare is None or fare < cheapest_fare):
            cheapest_fare = fare
            cheapest_class = code

    duration_str = t.get("duration") or t.get("journeyDuration") or ""
    duration_min = 0
    if ":" in duration_str:
        try:
            h, m = duration_str.split(":")[:2]
            duration_min = int(h) * 60 + int(m)
        except Exception:
            pass

    return {
        "train_number": t.get("trainNumber") or t.get("trainNo") or t.get("train_number"),
        "train_name": t.get("trainName") or t.get("name"),
        "train_type": t.get("trainType") or t.get("type"),
        "from_station_code": t.get("sourceStationCode") or t.get("fromStation") or t.get("srcStnCode"),
        "from_station_name": t.get("sourceStationName") or t.get("srcStnName"),
        "to_station_code": t.get("destinationStationCode") or t.get("toStation") or t.get("dstStnCode"),
        "to_station_name": t.get("destinationStationName") or t.get("dstStnName"),
        "departure_time": t.get("departureTime") or t.get("departure") or t.get("depTime"),
        "arrival_time": t.get("arrivalTime") or t.get("arrival") or t.get("arrTime"),
        "duration": duration_str,
        "duration_min": duration_min,
        "distance_km": t.get("distanceKm") or t.get("distance"),
        "cheapest_fare": cheapest_fare,
        "cheapest_class": cheapest_class,
        "available_classes": available_classes,
        "running_days": t.get("runningDays") or t.get("daysOfRun") or [],
        "pantry": t.get("pantryAvailable", False),
        "source": "confirmtkt",
    }


class RailwayService:
    """Indian Railway search via ConfirmTKT — India-only domestic routes."""

    def city_to_code(self, city_name: str) -> str | None:
        return city_to_station_code(city_name)

    async def search(
        self,
        origin: str,
        destination: str,
        date: str,
        from_code: str | None = None,
        to_code: str | None = None,
        limit: int = 5,
    ) -> dict:
        """
        Search trains between two cities. Caller can provide station codes directly
        (from Gemini) to avoid the city→code lookup.
        Returns {"trains": [...], "from_code": str, "to_code": str, "resolved": bool}
        """
        fc = from_code or city_to_station_code(origin)
        tc = to_code or city_to_station_code(destination)

        if not fc or not to_code and not tc:
            return {
                "trains": [], "from_code": fc, "to_code": tc,
                "resolved": False,
                "note": "No railway station found — this area may not have rail connectivity",
            }

        trains = await search_trains(fc, tc or "", date, limit=limit)
        return {
            "trains": trains,
            "from_code": fc,
            "to_code": tc,
            "resolved": True,
        }
