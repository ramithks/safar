"""
The hotel's own photographs, and what a page says about them.

Two things had to be true before a gallery was worth building. The photos had
to be describable — a hotel page whose eleven images all carry `alt=""` is a
page a screen reader reads as eleven nothings, and §26 asks for alt text rather
than for an attribute that is present. And nothing already stored could break:
every `hotel_websites` row written before this held its gallery as bare URL
strings, and a hotelier may be editing one.

The rule the derivation follows is that **nobody fills in twenty-four alt-text
boxes**. A required field here produces either empty alts or twenty-four copies
of the hotel's name. So the hotel's own facts write the sentence, and a
hotelier who wants better overwrites it.
"""
import pytest

from app.api.v1.pms import endpoint
from app.services.pms import website


HOTEL = "Casa Mandala"
CITY = "Anjuna"


# ── the two shapes a stored gallery can have ─────────────────────────────────

def test_a_gallery_stored_as_bare_urls_still_reads():
    """
    Every row written before the builder had a gallery. Reading them costs
    nothing and means no migration touches a jsonb column somebody may be
    editing at the time.
    """
    item = website.gallery_item("https://cdn.example/a.jpg")
    assert item == {"url": "https://cdn.example/a.jpg", "category": "other",
                    "caption": None, "alt": None}


def test_an_unknown_category_becomes_other_rather_than_itself():
    """
    The vocabulary is closed because a category drives a heading and a derived
    sentence. A value nobody designed would render as a section title nobody
    wrote.
    """
    assert website.gallery_item(
        {"url": "https://cdn.example/a.jpg", "category": "helipad"}
    )["category"] == "other"


@pytest.mark.parametrize("value", [None, "", "   ", 42, {}, {"url": ""}, {"url": "  "}])
def test_an_entry_with_no_photograph_in_it_is_dropped(value):
    assert website.gallery_item(value) is None


def test_the_same_photograph_twice_is_stored_once():
    config = website._normalise(
        {"gallery": ["https://cdn.example/a.jpg",
                     {"url": "https://cdn.example/a.jpg", "category": "pool"},
                     {"url": "https://cdn.example/b.jpg"}]},
        {},
    )
    assert [i["url"] for i in config["gallery"]] == [
        "https://cdn.example/a.jpg", "https://cdn.example/b.jpg",
    ]


def test_a_gallery_cannot_grow_without_bound():
    config = website._normalise(
        {"gallery": [f"https://cdn.example/{n}.jpg" for n in range(60)]}, {},
    )
    assert len(config["gallery"]) == website.MAX_GALLERY


def test_the_gallery_is_still_an_allow_list():
    """
    The config is served to the public. A field nobody designed is a field
    nobody escaped, and that was true of the gallery's entries the moment they
    stopped being strings.
    """
    item = website.gallery_item({
        "url": "https://cdn.example/a.jpg",
        "onerror": "alert(1)",
        "category": "pool",
    })
    assert set(item) == {"url", "category", "caption", "alt"}


# ── what a photograph is called ──────────────────────────────────────────────

def test_what_the_hotelier_wrote_wins():
    alt = website.photo_alt(
        {"alt": "Sea-facing balcony, second floor", "caption": "Room 204",
         "category": "room"},
        hotel=HOTEL, city=CITY,
    )
    assert alt == "Sea-facing balcony, second floor"


def test_a_caption_written_for_a_human_is_better_than_a_derived_sentence():
    alt = website.photo_alt({"caption": "Sunset from the pool", "category": "pool"},
                            hotel=HOTEL, city=CITY)
    assert alt == "Sunset from the pool"


def test_a_room_is_described_by_its_own_name():
    """
    More specific than any category, and specific is the entire value of alt
    text. "Deluxe Balcony at Casa Mandala, Anjuna" is a phrase somebody
    searches; "hotel room" is not.
    """
    alt = website.photo_alt({}, hotel=HOTEL, city=CITY, subject="Deluxe Balcony")
    assert alt == "Deluxe Balcony at Casa Mandala, Anjuna"


@pytest.mark.parametrize("category,expected", [
    ("pool", "The pool at Casa Mandala, Anjuna"),
    ("lobby", "The lobby at Casa Mandala, Anjuna"),
    ("exterior", "Outside Casa Mandala, Anjuna"),
    ("dining", "Dining at Casa Mandala, Anjuna"),
    ("surroundings", "Around Casa Mandala, Anjuna"),
    ("other", "Casa Mandala, Anjuna"),
])
def test_a_category_writes_a_true_sentence(category, expected):
    assert website.photo_alt({"category": category}, hotel=HOTEL, city=CITY) == expected


def test_alt_text_is_never_empty():
    """
    The whole point. There is no input to this function that produces an
    unlabelled image, including the one where the hotel has told us nothing but
    its name.
    """
    for item in ({}, {"alt": ""}, {"caption": "   "}, {"category": None},
                 {"alt": None, "caption": None, "category": "helipad"}):
        assert website.photo_alt(item, hotel=HOTEL).strip()


def test_a_hotel_with_no_city_is_still_described():
    assert website.photo_alt({"category": "pool"}, hotel=HOTEL) == "The pool at Casa Mandala"


# ── what the public payload carries ──────────────────────────────────────────

def test_every_gallery_photo_leaves_with_alt_text_resolved():
    """
    Resolved on the server so that every surface reading this payload describes
    an image the same way — and so a second renderer cannot quietly ship empty
    alts by forgetting to derive them.
    """
    content = endpoint._content_with_alts(
        {"gallery": ["https://cdn.example/a.jpg",
                     {"url": "https://cdn.example/p.jpg", "category": "pool"}]},
        HOTEL, CITY,
    )
    assert [i["alt"] for i in content["gallery"]] == [
        "Casa Mandala, Anjuna", "The pool at Casa Mandala, Anjuna",
    ]


def test_a_hotel_with_no_website_row_still_answers():
    assert endpoint._content_with_alts(None, HOTEL, CITY)["gallery"] == []


def test_room_photo_meta_follows_the_photos_and_not_the_captions():
    """
    `photos[]` decides which photographs exist and in what order; `photo_meta`
    is a map beside it. A caption for a URL the hotelier has since removed must
    not reach the page, and a photo added without a caption must still be
    described.
    """
    meta = endpoint._room_photo_meta(
        {
            "name": "Deluxe Balcony",
            "photos": ["https://cdn.example/1.jpg", "https://cdn.example/2.jpg"],
            "photo_meta": {
                "https://cdn.example/1.jpg": {"caption": "Balcony at sunrise"},
                "https://cdn.example/removed.jpg": {"caption": "a photo nobody shows"},
            },
        },
        HOTEL, CITY,
    )

    assert [m["url"] for m in meta] == [
        "https://cdn.example/1.jpg", "https://cdn.example/2.jpg",
    ]
    assert meta[0]["alt"] == "Balcony at sunrise"
    assert meta[1]["alt"] == "Deluxe Balcony at Casa Mandala, Anjuna"
    assert all(m["alt"] for m in meta)


def test_a_room_with_no_photographs_has_no_entries():
    assert endpoint._room_photo_meta({"name": "Garden Standard"}, HOTEL, CITY) == []


# ── the shapes the API accepts and returns ───────────────────────────────────

def test_the_save_response_can_carry_what_the_save_accepted():
    """
    The 500 this exists for. `WebsiteConfigOut.gallery` was `list[str]` while
    the column had started holding objects, so a save that **had already
    succeeded** answered 500 — the write landed, the response failed
    validation, and the hotelier saw an error over a change that was live.
    Nothing in the unit tests could see it; the round trip through the real API
    was what found it.
    """
    from app.api.v1.pms import schemas

    config = schemas.WebsiteConfigOut(
        propertyId="p", template="boutique", status="draft",
        gallery=[{"url": "https://cdn.example/a.jpg", "category": "pool",
                  "caption": "Sunset", "alt": None}],
    )
    assert config.gallery[0].category == "pool"


def test_a_client_that_only_has_urls_may_still_send_them():
    """
    The builder sends objects. A script, an importer, or any caller written
    before this does not, and refusing them would break a shape the API has
    accepted since the website shipped.
    """
    from app.api.v1.pms import schemas

    request = schemas.WebsiteUpdateRequest(
        gallery=["https://cdn.example/a.jpg",
                 {"url": "https://cdn.example/b.jpg", "category": "lobby"}],
    )
    stored = website._normalise(
        request.model_dump(exclude_none=True, exclude={"template"}), {},
    )
    assert [i["category"] for i in stored["gallery"]] == ["other", "lobby"]
