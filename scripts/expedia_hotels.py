import json
from curl_cffi import requests

HEADERS = {
    "accept": "application/json, multipart/mixed",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "max-age=0",
    "client-info": "shopping-pwa,522c3c77340ff756eea2a318598836a9df5ea00a,us-west-2",
    "content-type": "application/json",
    "ctx-view-id": "e3474cad-1f7a-4c7f-b741-78d284632baf",
    "origin": "https://www.expedia.co.in",
    "priority": "u=1, i",
    "referer": "https://www.expedia.co.in/Hotel-Search?destination=Shangarh%2C%20Sainj%2C%20Himachal%20Pradesh%2C%20India&regionId=553248635930542770&latLong=31.744467%2C77.39881&flexibility=0_DAY&d1=2026-06-04&startDate=2026-06-04&d2=2026-06-06&endDate=2026-06-06&adults=2&rooms=1&typeaheadCollationId=db3a7afb-092c-445c-b50f-5de0ba0f48b1&sort=RECOMMENDED&theme=&userIntent=&semdtl=&categorySearch=&useRewards=false",
    "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "x-enable-apq": "true",
    "x-page-id": "page.Hotel-Search,H,20",
    "x-shopping-product-line": "lodging",
}

COOKIES = {
    "tpid": "v.1,27",
    "iEAPID": "0",
    "currency": "INR",
    "CRQS": "t|27`s|27`l|en_GB`c|INR",
    "CRQSS": "e|0",
    "linfo": "v.4,|0|0|255|1|0||||||||2057|0|0||0|0|0|-1|-1",
    "EG_ANONTOKEN": "ClAKJgokYmYzMzEwZGUtMzgyNi00Mzc0LTk3ZDMtZTk2NWQzNjEwNDQ2EiYKJDk5NDY5OWIxLTk5ZGQtNDlhMy1hNTVjLTVlODY2NGQ2MzBhYQ.LPstlEFF2dn9rJWW.GMvrpBwir8blqmz9_ZMdEUQUswXBWwq7yxN_8lztmuUjSpzS3kK6xajNQUJpMAbP-IvN5atFf2T-KvHAGYZKvaDGbDwRiQqo6wS4HWMCVQ56o79qwi1r_TdwWIDBdgvUwDm1v-KCqE5pyp_HTtCCb830eZj7rzVrDkWCn5xODporVAOhTthZyKP2nWdK9dZnfc9is1Gy-WoNFrIOukmaHq9T1x0fY7U3pah07x6p8VE.aGgkb8A64TU5OgxlBZdS9Q",
    "sdui-trips-enabled": "0",
    "HMS": "6c246252-65f0-35a7-acad-2f8bf3729952",
    "MC1": "GUID=44175266be3544e8ad969f5fe1836983",
    "DUAID": "44175266-be35-44e8-ad96-9f5fe1836983",
    "eg_ppid": "90fcecb2-2db9-47f4-8b17-c815d64cf887",
    "page_name": "Homepage",
    "pageVisited": "true",
}

PAYLOAD = {
    "variables": {
        "context": {
            "siteId": 27,
            "locale": "en_GB",
            "eapid": 0,
            "tpid": 27,
            "currency": "INR",
            "device": {"type": "DESKTOP"},
            "identity": {
                "duaid": "44175266-be35-44e8-ad96-9f5fe1836983",
                "authState": "ANONYMOUS",
            },
            "privacyTrackingState": "CAN_TRACK",
        },
        "criteria": {
            "primary": {
                "dateRange": {
                    "checkInDate": {"day": 4, "month": 6, "year": 2026},
                    "checkOutDate": {"day": 6, "month": 6, "year": 2026},
                },
                "destination": {
                    "regionName": "Shangarh, Sainj, Himachal Pradesh, India",
                    "regionId": "553248635930542770",
                    "coordinates": {"latitude": 31.744467, "longitude": 77.39881},
                    "pinnedPropertyId": None,
                    "propertyIds": None,
                    "mapBounds": None,
                },
                "rooms": [{"adults": 2, "children": []}],
            },
            "secondary": {
                "counts": [
                    {"id": "resultsStartingIndex", "value": 3},
                    {"id": "resultsSize", "value": 97},
                ],
                "booleans": [],
                "selections": [
                    {"id": "privacyTrackingState", "value": "CAN_TRACK"},
                    {"id": "productOffersId", "value": "afad5996-65c9-4cf0-845f-59e17c31f4d1"},
                    {"id": "searchId", "value": "7c3cb0e7-f1d7-4d36-a835-6f76a5eae639"},
                    {"id": "sort", "value": "RECOMMENDED"},
                    {"id": "useRewards", "value": "SHOP_WITHOUT_POINTS"},
                ],
                "ranges": [],
            },
        },
        "shoppingContext": {
            "multiItem": None,
            "queryTriggeredBy": "PAGE-LOAD",
            "typeaheadCollationId": "db3a7afb-092c-445c-b50f-5de0ba0f48b1",
        },
    },
    "operationName": "PropertyListingQuery",
    "extensions": {
        "persistedQuery": {
            "sha256Hash": "2e04ea7ec1acdac9235f792efa884ab17f70e513e73a286fbc67d852740a1c5f",
            "version": 1,
        }
    },
}

resp = requests.post(
    "https://www.expedia.co.in/graphql",
    headers=HEADERS,
    cookies=COOKIES,
    json=PAYLOAD,
    impersonate="chrome",
    timeout=30,
)

print(f"Status: {resp.status_code}")
print(json.dumps(resp.json(), indent=2, ensure_ascii=False)[:3000])
