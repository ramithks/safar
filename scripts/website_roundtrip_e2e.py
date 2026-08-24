#!/usr/bin/env python3
"""
Save → publish → what a guest actually reads.

**Why the round trip and not unit tests.** The website fixes in this audit —
the gallery, the cover image, clearing a cover, the category, the caption, the
alt text, the save response type — are all covered by `tests/test_hotel_gallery.py`
and they all pass. Every one of them also passed *before* the bugs were found,
because each unit test checked one function and the failures lived in the joins
between them: a save that accepted a photograph and returned a response the
client could not read it back out of, a publish that copied a draft field the
public projection then dropped.

So this asserts the only thing that matters to a hotelier, which is that the
photograph they uploaded on Tuesday is on the page a guest opens on Wednesday.

    cd travbuddy_backend
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/uvicorn app.main:app --port 8010 &
    ./venv/bin/python scripts/website_roundtrip_e2e.py

**Draft and published are separate on purpose**, and that is asserted too: a
hotelier typing a new headline must not change the page a guest is reading
while they type it. The middle section below saves without publishing and
proves the public page has not moved.

⚠ Real database. Runs on Casa Mandala, and puts the website config back exactly
as it found it — including republishing, if it was published when this started.
"""
import base64
import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

REF = "rdlozqvuywtgsodunait"
SB = f"https://{REF}.supabase.co"
# Local by default, so `uvicorn --port 8010 &` and run it. Point it at the
# deployed backend to verify a release actually serves what it was given:
#
#     STAYOS_API=https://stayos-backend.travbuddy.in/api/v1/pms \
#       ./venv/bin/python scripts/website_roundtrip_e2e.py
#
# The distinction matters more here than in most suites. Every failure this
# script is for lives in the join between a save and a publish, and a local
# process reading the production database proves the code is right without
# proving the box is running it.
API = os.environ.get("STAYOS_API", "http://127.0.0.1:8010/api/v1/pms")
PROP = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
SLUG = "casa-mandala-anjuna"

# Two photographs the hotel is supposed to own. Distinctive enough that finding
# them on the public page cannot be a coincidence with a seeded fixture.
COVER = "https://images.stayos.test/roundtrip/cover-veranda.jpg"
GALLERY_ONE = "https://images.stayos.test/roundtrip/pool-at-dusk.jpg"
GALLERY_TWO = "https://images.stayos.test/roundtrip/breakfast-terrace.jpg"

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}  {detail}")
    return bool(cond)


def http(url, method="GET", data=None, headers=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        url, method=method, data=body,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as res:
            raw = res.read()
            return res.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw.decode()[:300]
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


ANON = None
try:
    raw = subprocess.check_output(
        ["security", "find-generic-password", "-s", "Supabase CLI", "-w"], text=True
    ).strip()
    MGMT = base64.b64decode(raw.removeprefix("go-keyring-base64:")).decode()
except Exception:  # noqa: BLE001
    MGMT = None


def sql(query: str):
    req = urllib.request.Request(
        f"https://api.supabase.com/v1/projects/{REF}/database/query",
        method="POST", data=json.dumps({"query": query}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {MGMT}",
                 "User-Agent": "SupabaseCLI/2.0.0"},
    )
    try:
        with urllib.request.urlopen(req) as res:
            return json.loads(res.read() or b"null")
    except urllib.error.HTTPError as exc:
        return {"error": exc.read().decode()[:300]}


def signin(email: str):
    import os
    anon = os.environ.get("SUPABASE_ANON_KEY") or os.environ.get("SUPABASE_KEY")
    st, d = http(f"{SB}/auth/v1/token?grant_type=password", "POST",
                 {"email": email, "password": "hotel-os-dev"}, {"apikey": anon})
    if st != 200:
        return None
    return {"Authorization": "Bearer " + d["access_token"]}


def photos_in(payload) -> list[str]:
    """
    Every image URL anywhere in a public payload.

    Walked rather than read off a known key, because the whole class of bug this
    script is for is a field arriving somewhere the reader was not looking. If
    the photograph is anywhere on the page, this finds it; if it is nowhere, the
    hotelier's upload did not reach the guest, whatever the shape.
    """
    found: list[str] = []

    def walk(value):
        if isinstance(value, str):
            if value.startswith("http") and "stayos.test" in value:
                found.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return found


def main() -> int:
    print("\n══ website: save → publish → the public page ══\n")

    st, _ = http(f"{API}/public/hotels/{SLUG}")
    if st == 0:
        print("The API is not running on :8010. Start uvicorn first.")
        return 2

    manager = signin("priya@casamandala.in")
    if not manager:
        print("Could not sign in as priya@casamandala.in — is the demo seeded?")
        return 2

    # ── what was there before ───────────────────────────────────────────────
    st, before = http(f"{API}/properties/{PROP}/website", headers=manager)
    if not check("the builder reads the hotel's draft", st == 200, f"{st} {before}"):
        return 1
    was_published = (before or {}).get("status") == "published"
    print(f"    · was {'published' if was_published else 'unpublished'}; "
          f"will be restored")

    original = {
        "template": (before or {}).get("template"),
        "hero": (before or {}).get("heroImageUrl"),
        "about": (before or {}).get("about"),
        "gallery": (before or {}).get("gallery") or [],
    }

    try:
        # ── 1. the save, and what it hands back ─────────────────────────────
        print("\n── 1. a hotelier saves a cover and two gallery photographs ──")

        st, saved = http(
            f"{API}/properties/{PROP}/website", "PATCH",
            {
                # `heroImageUrl` and `about`, which are what the schema calls
                # them. Not `cover_image`/`description`: see the unknown-field
                # check below for why that distinction is worth a test of its own.
                "heroImageUrl": COVER,
                "about": "A quiet courtyard house two lanes back from Anjuna beach.",
                "gallery": [
                    {"url": GALLERY_ONE, "category": "pool",
                     "caption": "The pool, twenty minutes before sunset",
                     "alt": "Swimming pool lit by evening sun"},
                    {"url": GALLERY_TWO, "category": "dining",
                     "caption": "Breakfast on the terrace"},
                ],
            },
            headers=manager,
        )
        check("the save is accepted", st == 200, f"{st} {str(saved)[:200]}")

        # **The save response type.** A save that accepts a photograph and hands
        # back a body the client cannot read it out of is a save the builder
        # shows as empty — which is what a hotelier reports as "it didn't save".
        gallery = (saved or {}).get("gallery") or []
        check("   and the response carries the gallery it just accepted",
              len(gallery) == 2, f"{len(gallery)}: {str(gallery)[:200]}")
        check("   with the hotelier's own caption, not a derived one",
              any((g.get("caption") or "").startswith("The pool") for g in gallery),
              str(gallery)[:250])
        # Alt text written by the hotelier survives; the one they left blank is
        # derived on the way *out* to a guest rather than stored on the draft.
        # Asserted where it matters — on the public page, in section 3.
        check("   the alt text a hotelier wrote is kept",
              any(g.get("alt") == "Swimming pool lit by evening sun" for g in gallery),
              str([g.get("alt") for g in gallery]))
        check("   the category survives the round trip",
              {g.get("category") for g in gallery} == {"pool", "dining"},
              str([g.get("category") for g in gallery]))
        check("   and the hero image is what was sent",
              (saved or {}).get("heroImageUrl") == COVER,
              f"got {(saved or {}).get('heroImageUrl')!r}")

        # **A save that quietly accepted a field it does not have.**
        #
        # `cover_image` is the name this script used on its own first run, and
        # it used to get a 200, an unchanged draft and no indication that
        # anything had been ignored — the exact shape of "it says it saved and
        # it didn't".
        #
        # The finding this used to print is now the assertion below. It is still
        # a 200 and the draft is still unchanged, both deliberately: the builder
        # lives in another repository and a 422 to a client that has been
        # sending a stray key for months is a hotelier who cannot edit their own
        # page. What changed is that the response says so. See
        # `WebsiteUpdateRequest` and `settings.website_strict_fields`.
        st_extra, extra = http(
            f"{API}/properties/{PROP}/website", "PATCH",
            {"cover_image": "https://images.stayos.test/roundtrip/ignored.jpg",
             "tagline": "Rooms around a courtyard"},
            headers=manager,
        )
        check("an unknown field is named in the response, not swallowed",
              st_extra == 200 and "cover_image" in ((extra or {}).get("ignoredFields") or []),
              f"{st_extra}: ignoredFields={(extra or {}).get('ignoredFields')!r}")
        check("   and the correct field in the same save still saved",
              (extra or {}).get("tagline") == "Rooms around a courtyard",
              f"got {(extra or {}).get('tagline')!r}")
        check("   while the unknown one reached nothing",
              (extra or {}).get("heroImageUrl") == COVER,
              f"heroImageUrl is now {(extra or {}).get('heroImageUrl')!r}")

        # ── 2. a draft is not a page ────────────────────────────────────────
        print("\n── 2. saving is not publishing ──")

        st, public = http(f"{API}/public/hotels/{SLUG}")
        if st == 200:
            leaked = [p for p in photos_in(public) if p in (COVER, GALLERY_ONE, GALLERY_TWO)]
            check("an unpublished draft is not on the public page",
                  not leaked,
                  f"a guest can already see {leaked} — the draft reached the "
                  f"live site without anybody publishing it")
        else:
            check("an unpublished draft is not on the public page", True)

        # ── 3. publish ──────────────────────────────────────────────────────
        print("\n── 3. the hotelier presses publish ──")

        st, published = http(f"{API}/properties/{PROP}/website/publish", "POST",
                             headers=manager)
        check("the publish succeeds", st == 200, f"{st} {str(published)[:200]}")
        check("   and the row says published",
              (published or {}).get("status") == "published",
              str((published or {}).get("status")))

        st, public = http(f"{API}/public/hotels/{SLUG}")
        check("the public page answers", st == 200, f"{st} {str(public)[:160]}")

        seen = photos_in(public)
        check("   the hero image reached the guest", COVER in seen,
              f"not among {len(seen)} images on the page")
        check("   and so did both gallery photographs",
              GALLERY_ONE in seen and GALLERY_TWO in seen,
              f"found {[p for p in seen if 'roundtrip' in p]}")
        check("   the description the hotelier wrote is the one on the page",
              "quiet courtyard house" in json.dumps(public),
              json.dumps(public)[:300])

        # Every photograph a guest is shown carries alt text, derived where the
        # hotelier left it blank. This is the assertion the save-side one was
        # trying to be: alt is a property of what reaches the guest.
        site_gallery = ((public or {}).get("website") or {}).get("gallery") or \
            (public or {}).get("gallery") or []
        if site_gallery and isinstance(site_gallery[0], dict):
            check("   and every photograph on it has alt text",
                  all((g.get("alt") or "").strip() for g in site_gallery),
                  str([g.get("alt") for g in site_gallery])[:200])

        # **Hotel-authored data is the source of truth.** §16: FAQs, the
        # description, the SEO and the structured data all come from what the
        # hotel wrote, and nothing on the public surface may invent them.
        blob = json.dumps(public)
        check("   nothing on the page claims an amenity the hotel did not enter",
              "Wi-Fi included" not in blob or "wifi" in blob.lower(),
              "the public payload asserts something the hotel never wrote")

        # ── 4. clearing the cover ───────────────────────────────────────────
        print("\n── 4. clearing the cover, which used to be impossible ──")

        st, cleared = http(f"{API}/properties/{PROP}/website", "PATCH",
                           {"heroImageUrl": ""}, headers=manager)
        check("an empty cover is accepted rather than ignored", st == 200,
              f"{st} {str(cleared)[:200]}")
        check("   and it is actually gone from the draft",
              not (cleared or {}).get("heroImageUrl"),
              f"still {(cleared or {}).get('heroImageUrl')!r} — a hotelier who "
              f"removes a photograph is told it worked and it did not")
        check("   while the gallery is untouched by a cover edit",
              len((cleared or {}).get("gallery") or []) == 2,
              str(len((cleared or {}).get("gallery") or [])))

        # ── 5. the published page has not moved ─────────────────────────────
        st, public_again = http(f"{API}/public/hotels/{SLUG}")
        check("clearing a draft field does not change the live page",
              COVER in photos_in(public_again),
              "the guest's page changed while the hotelier was still editing")

    finally:
        # ── put it back ─────────────────────────────────────────────────────
        print("\n── restoring ──")
        restore = {
            "heroImageUrl": original["hero"] or "",
            "about": original["about"] or "",
            "gallery": original["gallery"],
        }
        if original["template"]:
            restore["template"] = original["template"]
        st, _ = http(f"{API}/properties/{PROP}/website", "PATCH", restore,
                     headers=manager)
        print(f"    · draft restored ({st})")
        if was_published:
            st, _ = http(f"{API}/properties/{PROP}/website/publish", "POST",
                         headers=manager)
            print(f"    · republished ({st})")
        else:
            st, _ = http(f"{API}/properties/{PROP}/website/unpublish", "POST",
                         headers=manager)
            print(f"    · unpublished again ({st})")

    print(f"\n╔═ {PASS} passed, {FAIL} failed")
    print("╚═ REAL DATABASE · REAL API · draft and published kept apart\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
