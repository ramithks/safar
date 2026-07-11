"""
EaseMyTrip Flights API integration.
POST https://flightservice-node.easemytrip.com/AirAvail_Lights/AirBus_New

Booking URL format (confirmed):
  https://www.easemytrip.com/flight-search/listing
    ?srch={IATA}-{CityName}-India|{IATA}-{CityName}-India|{DD/MM/YYYY}
    &px={adults}-{children}-{infants}&cbn=0&ar=undefined
    &isow=true&isdm=true&lang=en-us&IsDoubleSeat=false&CCODE=IN&curr=INR&apptype=B2C
"""
import asyncio
import base64
import logging
import re
import time
import urllib.parse
from app.services.curl_pool import get_curl_session, CurlAsyncSession
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from app.services.scraper_proxy import with_proxy_fallback, ScraperBlocked
from app.services.geo import is_domestic_iata

logger = logging.getLogger(__name__)

_BASE = "https://flightservice-node.easemytrip.com/AirAvail_Lights/AirBus_New"
_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json",
    "origin": "https://www.easemytrip.com",
    "referer": "https://www.easemytrip.com/",
    "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
}

_CABIN_CODE = {
    "economy": 0, "business": 1, "first": 2, "premium_economy": 3,
}

# ── Search token (TKN) handshake ─────────────────────────────────────────────
# As of 2026-06 the AirBus_New search rejects requests without a valid TKN
# ("TOKEN NOT VALID"). The EMT SPA mints one via a two-step call to its etoken
# service, then passes it as payload.TKN. Reproduced from the flight-search
# Angular bundle (chunk 732):
#   1. POST etoken/UMS  (header ATK, body "{}")        → {ITK, b}  (both opaque)
#   2. POST etoken/PSP  (header autk=enc("EMT|<static>|<b>|<ITK>"),
#                        body {UserName:"EMT", ITK:enc(ITK), IP:enc(b)}) → {STK}
# enc = AES-128-CBC(key=iv=encKeySrchV1), PKCS7, base64. STK is the TKN.
_UMS_URL    = "https://gi.easemytrip.com/etm/api/etoken/UMS"
_PSP_URL    = "https://gi.easemytrip.com/etm/api/etoken/PSP"
_ENC_KEY    = b"hylW@zmEQdG@4Idr"   # encKeySrchV1 (key == iv)
_ATK        = "c0ruxuOD/XqyNXB3kRAqvRUliyqhyFOOUwgDTCP1tLx09TRNEbblvRQLgfPz2ivf"
_PSP_STATIC = "EMTWvggk6zYLVynBA56C3aNn4HLREMHV9bEP9Q"
_TOKEN_TTL  = 120.0   # seconds; EMT rejects tokens well before 300s (a 176s-old
                      # token returned "TOKEN NOT VALID" in prod), so keep our
                      # cache window comfortably under that. A stale token is also
                      # re-minted in-flight (see _do), so this is just to avoid a
                      # predictable first-call rejection.

# (token, fetched_at) — shared process-wide so the handshake runs ~once per TTL.
_token_cache: tuple[str, float] | None = None


def _enc_fsearch(value: str) -> str:
    """EMT EncryptionFSearch: AES-128-CBC with key==iv==encKeySrchV1, base64."""
    padder = PKCS7(128).padder()
    data = padder.update(value.encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(_ENC_KEY), modes.CBC(_ENC_KEY)).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode()


def _invalidate_token() -> None:
    global _token_cache
    _token_cache = None


async def _get_search_token(session: CurlAsyncSession) -> str | None:
    """Return a valid search TKN, minting (and caching) one if needed."""
    global _token_cache
    if _token_cache and (time.monotonic() - _token_cache[1]) < _TOKEN_TTL:
        return _token_cache[0]

    r1 = await session.post(
        _UMS_URL, headers={"Content-Type": "application/json", "ATK": _ATK},
        data="{}", timeout=12,
    )
    j1 = r1.json()
    itk, ip = j1.get("ITK"), j1.get("b")
    if not itk or not ip:
        raise ScraperBlocked(f"UMS no ITK/b: {str(j1)[:80]}")

    autk = _enc_fsearch(f"EMT|{_PSP_STATIC}|{ip}|{itk}")
    r2 = await session.post(
        _PSP_URL, headers={"Content-Type": "application/json", "autk": autk},
        json={"UserName": "EMT", "ITK": _enc_fsearch(itk), "IP": _enc_fsearch(ip)},
        timeout=12,
    )
    stk = (r2.json() or {}).get("STK")
    if not stk:
        raise ScraperBlocked(f"PSP no STK: {str(r2.text)[:80]}")

    _token_cache = (stk, time.monotonic())
    return stk

_AUTOSUGGEST_URL = "https://www.easemytrip.com/api/Flight/GetAutoSuggestNew"
_AUTOSUGGEST_HEADERS = {
    "accept": "application/json, text/javascript, */*; q=0.01",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json; charset=UTF-8",
    "origin": "https://www.easemytrip.com",
    "referer": "https://www.easemytrip.com/",
    "sec-ch-ua": '"Chromium";v="148"', "sec-ch-ua-mobile": "?0",
    "sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/148.0.0.0 Safari/537.36",
    "x-requested-with": "XMLHttpRequest",
}

# In-memory cache: IATA → (city_name_nospaces, country)
_CITY_CACHE: dict[str, tuple[str, str]] = {}


async def _resolve_iata_city(iata: str) -> tuple[str, str]:
    """
    Resolve IATA → (city_name, country) via EMT autosuggest.
    City name has spaces removed to match listing URL format (e.g. "NewDelhi").
    """
    key = iata.upper()
    if key in _CITY_CACHE:
        return _CITY_CACHE[key]

    async def _do(proxy):
        async with get_curl_session(impersonate="chrome", proxy=proxy) as s:
            r = await s.post(
                _AUTOSUGGEST_URL, headers=_AUTOSUGGEST_HEADERS,
                json={"Prefix": iata}, timeout=8,
            )
        return r.json()

    try:
        parsed = await with_proxy_fallback("emt_autosuggest", _do)
        items = parsed if isinstance(parsed, list) else []
        for item in items:
            city_field = item.get("City", "")   # e.g. "New Delhi(DEL)"
            if f"({key})" in city_field:
                city_name = item.get("CityName", key).strip().replace(" ", "")
                country   = item.get("Country", "India").strip()
                _CITY_CACHE[key] = (city_name, country)
                return city_name, country
    except Exception as e:
        logger.warning("[emt_autosuggest] IATA %r: %s", iata, e)

    # Fallback: use IATA as city name
    fallback = (key, "India")
    _CITY_CACHE[key] = fallback
    return fallback


async def _build_listing_url(
    from_iata: str,
    to_iata: str,
    depart_date: str,   # "YYYY-MM-DD"
    adults: int = 1,
    children: int = 0,
    infants: int = 0,
    cabin: int = 0,     # 0=Economy, 1=Business
) -> str:
    """
    Build the EaseMyTrip flight listing deep-link URL.
    srch format: {IATA}-{CityNoSpaces}-{Country}|{IATA}-{City}-{Country}|{DD/MM/YYYY}
    City names resolved live via EMT autosuggest (cached).
    """
    try:
        from datetime import datetime as _dt
        date_fmt = _dt.strptime(depart_date, "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        date_fmt = depart_date

    (from_city, from_country), (to_city, to_country) = await asyncio.gather(
        _resolve_iata_city(from_iata),
        _resolve_iata_city(to_iata),
    )

    srch = (
        f"{from_iata.upper()}-{from_city}-{from_country}"
        f"|{to_iata.upper()}-{to_city}-{to_country}"
        f"|{date_fmt}"
    )
    is_domestic = from_country == to_country

    return (
        "https://www.easemytrip.com/flight-search/listing"
        f"?srch={urllib.parse.quote(srch, safe='')}"
        f"&px={adults}-{children}-{infants}"
        f"&cbn={cabin}"
        f"&ar=undefined&isow=true"
        f"&isdm={'true' if is_domestic else 'false'}"
        f"&lang=en-us&IsDoubleSeat=false&CCODE=IN&curr=INR&apptype=B2C"
    )


async def search_flights(
    from_airport: str,
    to_airport: str,
    depart_date: str,
    adults: int = 1,
    children: int = 0,
    infants: int = 0,
    cabin_class: str = "economy",
    nonstop_only: bool = False,
) -> list[dict]:
    """Search one-way flights on EaseMyTrip (domestic or international)."""
    cabin_code = _CABIN_CODE.get(cabin_class.lower(), 0)
    ts = int(time.time() * 1000)
    # Previously hardcoded True, which broke every international route. Detect from
    # the airport codes; unknown codes default to domestic (the common case).
    is_domestic = is_domestic_iata(from_airport, to_airport)

    # TKN is required (see _get_search_token); filled in per-attempt inside _do
    # so a stale/rejected token can be re-minted on the proxy retry.
    payload = {
        "org": from_airport.upper(),
        "dept": to_airport.upper(),
        "adt": str(adults),
        "chd": str(children),
        "inf": str(infants),
        "deptDT": depart_date,
        "arrDT": "",
        "userid": "",
        "IsDoubelSeat": False,
        "isDomestic": is_domestic,
        "isOneway": True,
        "airline": "undefined",
        "Cabin": cabin_code,
        "currCode": "INR",
        "appType": 1,
        "isSingleView": False,
        "ResType": 2,
        "IsNBA": False,
        "CouponCode": "",
        "IsArmedForce": False,
        "AgentCode": "",
        "IsWLAPP": False,
        "IsFareFamily": False,
        "serviceid": "EMTSERVICE;",
        "serviceDepatment": "",
        "IpAddress": "",
        "LoginKey": "",
        "UUID": "",
        "TKN": "",
        "FareTypeUI": 0,
    }

    async def _do(proxy):
        # "TOKEN NOT VALID" is a stale/expired token, NOT an IP block — re-mint
        # and retry once on the *same* connection (near-instant on the direct
        # path) before with_proxy_fallback escalates to the slow proxy retries.
        # Jumping straight to proxy would re-run the UMS+PSP handshake over the
        # proxy and burn the whole 25s budget before the fresh-token search lands.
        async with get_curl_session(impersonate="chrome", proxy=proxy) as s:
            for token_attempt in range(2):
                payload["TKN"] = await _get_search_token(s)
                r = await s.post(
                    _BASE,
                    headers=_HEADERS,
                    params={"_": ts, "ngsw-bypass": "true"},
                    json=payload,
                    timeout=25,
                )
                r.raise_for_status()
                j = r.json()
                # A valid search returns a JSON object. A bare string
                # ("TOKEN NOT VALID", block bodies) means the token was rejected.
                if isinstance(j, str):
                    _invalidate_token()         # force a fresh mint next call
                    if token_attempt == 0:
                        continue                # retry once on this connection
                    # A freshly minted token was also rejected → treat as a real
                    # block so with_proxy_fallback rotates to a proxy exit IP.
                    raise ScraperBlocked(f"AirBus_New string response: {j[:60]}")
                return j

    try:
        # Cap the retry loop at 25s (< FlightService's 30s merge budget) so this
        # provider's proxy retries can never time out the shared gather.
        raw = await asyncio.wait_for(with_proxy_fallback("emt_flights", _do), timeout=25.0)
        listing_url = await _build_listing_url(from_airport, to_airport, depart_date, adults)
        flights = _extract_flights(raw, from_airport, to_airport, depart_date, adults, listing_url)
        if nonstop_only:
            flights = [f for f in flights if (f.get("stops") or 0) == 0]
        flights.sort(key=lambda f: (f.get("price_total") is None, f.get("price_total") or 999_999))
        return flights
    except Exception as e:
        logger.warning("[emt_flights] %s→%s on %s: %s", from_airport, to_airport, depart_date, e)
        return []


def _extract_flights(
    raw: dict,
    from_iata: str,
    to_iata: str,
    depart_date: str,
    adults: int,
    listing_url: str = "",
) -> list[dict]:
    """
    Handle both old and new EaseMyTrip response formats.

    New format (2025+): flights in j[0].s[*].b[*], legs in dctFltDtl[str(idx)]
    Old format: AirSearchResult.FlightDetails or FlightDataList.results
    """
    # ── New format: j[0].s (segment bundles) ─────────────────────────────────
    j = raw.get("j")
    dct = raw.get("dctFltDtl") or {}
    if j and dct:
        return _parse_new_format(raw, from_iata, to_iata, depart_date, adults, listing_url)

    # ── Old format: AirSearchResult ───────────────────────────────────────────
    result_block = raw.get("AirSearchResult") or {}
    items = result_block.get("FlightDetails") or result_block.get("FlightDetail") or []
    if items:
        return [_normalise_old(f, depart_date, from_iata, to_iata, adults, listing_url) for f in items]

    fdl = raw.get("FlightDataList") or {}
    items = fdl.get("results") or fdl.get("Results") or []
    if items:
        return [_normalise_old(f, depart_date, from_iata, to_iata, adults, listing_url) for f in items]

    if isinstance(raw, list):
        return [_normalise_old(f, depart_date, from_iata, to_iata, adults, listing_url) for f in raw]

    return []


def _parse_new_format(
    raw: dict,
    from_iata: str,
    to_iata: str,
    depart_date: str,
    adults: int,
    listing_url: str = "",
) -> list[dict]:
    """
    Parse the 2025+ EaseMyTrip format.
    Flights live in j[0].s[*].b[*]; leg details in dctFltDtl[str(idx)].
    Prices are not returned per-flight — booking URL still constructed.
    """
    j       = raw.get("j") or []
    dct     = raw.get("dctFltDtl") or {}
    d_field = raw.get("d") or {}

    # Build internal_code → iata map from d field: {"DEL|HDO": 28, "GOI|GOX": 39}
    internal_to_iata: dict[str, str] = {}
    for key in d_field:
        parts = key.split("|")
        if len(parts) == 2:
            iata_side, internal_side = parts
            internal_to_iata[internal_side] = iata_side

    def _resolve(code: str) -> str:
        return internal_to_iata.get(code, code)

    segments = (j[0].get("s") or []) if j else []

    # Build booking URL base for EMT search page
    emt_search_url = listing_url  # pre-built by caller

    results: list[dict] = []
    seen: set[str] = set()

    for seg in segments:
        bundle = (seg.get("b") or [{}])[0]
        fl_indices = bundle.get("FL") or []
        if not fl_indices:
            continue

        stp_raw = bundle.get("stp")
        stops = int(stp_raw) if stp_raw is not None else (len(fl_indices) - 1)

        first_leg = dct.get(str(fl_indices[0]))
        last_leg  = dct.get(str(fl_indices[-1]))
        if not first_leg:
            continue

        ac  = first_leg.get("AC", "")
        fn  = first_leg.get("FN", "")
        dedup_key = f"{ac}{fn}_{first_leg.get('DTM')}"
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        from_code = _resolve(first_leg.get("OG", ""))
        to_code   = _resolve((last_leg or first_leg).get("DT", ""))

        dep_date_raw = first_leg.get("DDT", "")
        arr_date_raw = (last_leg or first_leg).get("ADT", "")
        dep_time = first_leg.get("DTM", "")
        arr_time = (last_leg or first_leg).get("ATM", "")

        dep_dt = _parse_emt_datetime(dep_date_raw, dep_time)
        arr_dt = _parse_emt_datetime(arr_date_raw, arr_time)

        duration_str = bundle.get("JyTm") or first_leg.get("DUR") or ""
        duration_min = _parse_dur(duration_str)

        # Prices are at segment level, not bundle level
        # STFA (Sub Total Fare Adult) from lstFr[0] is the canonical total price
        lst_fr   = seg.get("lstFr") or []
        stfa     = (lst_fr[0].get("STFA") if lst_fr else None)
        base_fare = seg.get("AP")      # adult base fare
        total_tax = seg.get("TT")      # total tax
        total_fare = stfa or seg.get("TF")   # total = base + tax

        results.append({
            "flight_number": f"{ac}{fn}".strip(),
            "airline": first_leg.get("FlightName") or ac,
            "airline_code": ac,
            "from_airport": from_code,
            "to_airport": to_code,
            "departure_date": dep_date_raw,
            "departure_time": dep_dt,
            "arrival_date": arr_date_raw,
            "arrival_time": arr_dt,
            "duration_min": duration_min,
            "stops": stops,
            "cabin_class": first_leg.get("CB", "ECONOMY"),
            "seats_available": _safe_int(first_leg.get("SA")),
            "baggage_kg": _safe_int(first_leg.get("BW")),
            "price_total": _safe_int(total_fare),
            "price_base_fare": _safe_int(base_fare),
            "price_tax": _safe_int(total_tax),
            "currency": "INR",
            "booking_url": emt_search_url,
            "source": "easemytrip",
        })

    return results


def _parse_emt_datetime(date_str: str, time_str: str) -> str | None:
    """Convert 'Mon-15Jun2026' + '18:40' → ISO-like string."""
    try:
        from datetime import datetime as _dt
        # EMT date formats seen: "Mon-15Jun2026"
        cleaned = re.sub(r"^[A-Za-z]{3}-", "", date_str)  # strip "Mon-"
        d = _dt.strptime(cleaned, "%d%b%Y")
        h, m = time_str.split(":")
        return d.replace(hour=int(h), minute=int(m)).isoformat()
    except Exception:
        return f"{date_str}T{time_str}" if date_str and time_str else None


def _parse_dur(s: str) -> int | None:
    """Parse '02h 35m', '2:35' etc. → minutes."""
    if not s:
        return None
    h = re.search(r"(\d+)\s*h", s)
    m = re.search(r"(\d+)\s*m", s)
    if h or m:
        return (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0)
    if ":" in s:
        parts = s.split(":")
        try:
            return int(parts[0]) * 60 + int(parts[1])
        except Exception:
            pass
    return None


def _safe_int(v) -> int | None:
    try:
        return int(v) if v is not None and v != "" else None
    except Exception:
        return None


def _normalise_old(f: dict, depart_date: str, from_iata: str, to_iata: str, adults: int, listing_url: str = "") -> dict:
    """Normalize old EaseMyTrip response format (pre-2025)."""
    airline_code = f.get("al") or f.get("AirlineCode") or ""
    airline_name = f.get("aln") or f.get("AirlineName") or airline_code
    flight_no    = f.get("fn") or f.get("FlightNo") or ""
    if flight_no and airline_code and not flight_no.startswith(airline_code):
        flight_no = f"{airline_code}-{flight_no}"

    origin = f.get("oa") or f.get("org") or f.get("Origin") or from_iata
    dest   = f.get("da") or f.get("dept") or f.get("Dest") or to_iata
    dep_time = f.get("dt") or f.get("DeptTime") or ""
    arr_time = f.get("at") or f.get("ArrTime") or ""

    dur_raw = f.get("dur") or f.get("Duration") or 0
    duration_min = _parse_dur(str(dur_raw)) if dur_raw else None

    fare_block = f.get("f") or {}
    if not fare_block:
        fare_block = {"bf": f.get("bf") or 0, "tax": f.get("tax") or 0, "tf": f.get("tf") or 0}

    base  = fare_block.get("bf") or 0
    tax   = fare_block.get("tax") or 0
    total = fare_block.get("tf") or (base + tax) or None

    try:
        from datetime import datetime as _dt
        d = _dt.strptime(depart_date, "%Y-%m-%d")
        date_fmt = d.strftime("%d-%b-%Y")
    except Exception:
        date_fmt = depart_date

    return {
        "flight_number": flight_no,
        "airline": airline_name,
        "airline_code": airline_code,
        "from_airport": str(origin).upper(),
        "to_airport": str(dest).upper(),
        "departure_time": dep_time,
        "arrival_time": arr_time,
        "duration_min": duration_min,
        "stops": f.get("s") or f.get("stops") or 0,
        "cabin_class": "economy",
        "price_base_fare": int(base) if base else None,
        "price_tax": int(tax) if tax else None,
        "price_total": int(total) if total else None,
        "currency": "INR",
        "seats_available": f.get("sa") or f.get("SeatsAvailable"),
        "is_refundable": bool(f.get("refund") or f.get("IsRefundable")),
        "booking_url": listing_url,
        "source": "easemytrip",
    }
