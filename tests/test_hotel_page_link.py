"""
Carrying a conversation onto the website instead of restarting it there.

A guest spends six messages settling on 11–15 September for four people, then
asks to see photos and rooms laid out. Send them a bare hotel URL and they
arrive at an empty date picker — and the most common thing to do with an empty
date picker is close the tab.

**The model names a room; it never names a URL.** Same property as
`send_room_photos`. Every part of this link is composed on the server: the
origin from settings, the path from this property's slug, the query from this
conversation's stored session. There is no argument through which a model — or
a guest talking to one — can put a different address in front of somebody, and
that is why the tool takes no `url`, no `check_in` and no `guests`.

**And the dates on it are not the model's recollection.** They come from
`conversations.guest_profile`, which `session.patch_from` writes only from tool
inputs and outputs. A model that says "so that's the 10th to the 14th" moves
nothing; a `search_availability` call for those dates does.
"""
import asyncio

import pytest

from app.config import settings
from app.services.ai import registry
from app.services.ai.context import AgentContext, AgentPrincipal
from tests.fake_supabase import FakeDb

registry.load_all()

PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CONVERSATION = "c0c0c0c0-0001-4001-8001-000000000001"
DELUXE = "10000000-0000-4000-8000-000000000001"
SITE = settings.site_url.rstrip("/")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(monkeypatch) -> FakeDb:
    fake = FakeDb()
    for target in ("app.db.client", "app.services.ai.tools.stay"):
        monkeypatch.setattr(f"{target}.get_db", lambda: fake)
    fake.seed("properties", {
        "id": PROPERTY, "partner_id": PARTNER, "slug": "casa-mandala-anjuna",
        "name": "Casa Mandala", "status": "active",
    })
    fake.seed("hotel_websites", {
        "property_id": PROPERTY, "status": "published",
        "published": {"about": "A nine-room house near Ozran beach."},
    })
    fake.seed("room_types", {
        "id": DELUXE, "property_id": PROPERTY, "is_active": True,
        "name": "Deluxe Balcony", "slug": "deluxe-balcony", "code": "DLX",
    })
    fake.seed("conversations", {
        "id": CONVERSATION, "property_id": PROPERTY, "partner_id": PARTNER,
        "channel": "instagram", "guest_handle": "igsid_1", "handler": "ai",
        "guest_profile": {
            "language": "hinglish", "stage": "quoted",
            "check_in": "2026-09-11", "check_out": "2026-09-15",
            "adults": 4, "children": 0, "units": 2,
        },
    })
    return fake


def ctx(**over) -> AgentContext:
    principal = AgentPrincipal(
        agent_id="agent-1", agent_type="guest", partner_id=PARTNER,
        property_id=PROPERTY, display_name="Agent", autonomy_level=2,
        scopes=frozenset({"messaging"}),
    )
    base = AgentContext(
        principal=principal, property_id=PROPERTY, partner_id=PARTNER,
        model="test", conversation_id=CONVERSATION, actor="guest", run_id="run-1",
        tool_grants={"get_hotel_page_link":
                     {"tool_name": "get_hotel_page_link", "enabled": True}},
    )
    for key, value in over.items():
        setattr(base, key, value)
    return base


def call(args: dict | None = None, context: AgentContext | None = None):
    return run(registry.execute("get_hotel_page_link", args or {}, context or ctx()))


# ── the link ─────────────────────────────────────────────────────────────────

def test_the_dates_the_guest_already_gave_ride_on_the_link(db):
    url = call().output["url"]
    assert url.startswith(f"{SITE}/book/casa-mandala-anjuna?")
    for expected in ("checkin=2026-09-11", "checkout=2026-09-15",
                     "adults=4", "rooms=2"):
        assert expected in url, url


def test_a_zero_is_not_a_fact_worth_carrying(db):
    """Nobody needs `children=0` on a URL, and a page reads its own default."""
    assert "children" not in call().output["url"]


def test_a_thread_that_has_settled_nothing_gets_the_plain_page(db):
    db.rows("conversations")[0]["guest_profile"] = {"language": "en"}
    assert call().output["url"] == f"{SITE}/book/casa-mandala-anjuna"


def test_naming_a_room_opens_that_room(db):
    url = call({"room_type_id": DELUXE}).output["url"]
    assert url.startswith(f"{SITE}/book/casa-mandala-anjuna/rooms/deluxe-balcony?")


def test_a_room_with_no_slug_falls_back_to_its_code(db):
    db.rows("room_types")[0]["slug"] = None
    url = call({"room_type_id": DELUXE}).output["url"]
    assert "/rooms/dlx?" in url


# ── the safety property ──────────────────────────────────────────────────────

def test_the_model_cannot_name_a_url_or_a_date(db):
    spec = registry.REGISTRY["get_hotel_page_link"]
    assert set(spec.input_model.model_fields) == {"room_type_id"}


def test_another_hotels_room_does_not_deep_link(db):
    db.seed("room_types", {
        "id": "20000000-0000-4000-8000-00000000ffff",
        "property_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        "is_active": True, "name": "Theirs", "slug": "their-suite", "code": "THR",
    })
    url = call({"room_type_id": "20000000-0000-4000-8000-00000000ffff"}).output["url"]
    assert "their-suite" not in url
    assert url.startswith(f"{SITE}/book/casa-mandala-anjuna")


def test_a_room_that_is_off_sale_is_not_linked(db):
    db.rows("room_types")[0]["is_active"] = False
    assert "/rooms/" not in call({"room_type_id": DELUXE}).output["url"]


def test_another_conversations_session_cannot_be_read(db):
    """
    The session comes from this conversation and this property. A thread id
    from another hotel produces the plain page, never that hotel's guest's
    dates.
    """
    db.seed("conversations", {
        "id": "c0c0c0c0-0002-4002-8002-000000000002",
        "property_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        "partner_id": "bbbb", "channel": "instagram", "handler": "ai",
        "guest_profile": {"check_in": "2027-01-01", "check_out": "2027-01-05"},
    })
    url = call({}, ctx(conversation_id="c0c0c0c0-0002-4002-8002-000000000002")).output["url"]
    assert "2027-01-01" not in url


# ── the honest refusal ───────────────────────────────────────────────────────

def test_a_hotel_with_no_published_page_gets_no_link(db):
    """
    `resolve_published`'s rule, applied one step earlier: a hotelier drafting a
    site has not agreed to anybody seeing it, and a link to a 404 is worse than
    saying there is no page.
    """
    db.rows("hotel_websites")[0]["status"] = "draft"
    out = call().output
    assert out["url"] == ""
    assert "do not invent a website address" in out["instruction"].lower()


def test_a_hotel_with_no_website_row_at_all_gets_no_link(db):
    db.rows("hotel_websites").clear()
    assert call().output["url"] == ""


def test_a_suspended_property_gets_no_link(db):
    db.rows("properties")[0]["status"] = "suspended"
    assert call().output["url"] == ""


# ── how it is described ──────────────────────────────────────────────────────

def test_the_instruction_forbids_editing_the_link(db):
    instruction = call().output["instruction"].lower()
    assert "do not edit the link" in instruction


def test_it_says_which_parameters_it_filled_in(db):
    """So a reply can honestly say "your dates are already on it"."""
    assert call().output["prefilled"] == ["adults", "checkin", "checkout", "rooms"]
