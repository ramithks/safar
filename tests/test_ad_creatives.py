"""
Ad copy: what it may say, and what it may never claim.

An ad is the widest thing this product publishes. A campaign message reaches a
list; an ad reaches whoever Meta decides. So the bar here is the one from §29 —
never claim something the hotel did not supply — and these are the assertions
that hold it.
"""
import asyncio

from app.services.marketing import creatives, meta_ads


def run(coro):
    """This codebase has no pytest-asyncio; the convention is asyncio.run."""
    return asyncio.run(coro)

HOTEL = {"name": "Casa Mandala", "city": "Anjuna"}
ROOM = {
    "name": "Garden Standard",
    "description": (
        "A corner room on the upper floor with a private balcony over the "
        "garden, a king bed and a walk-in rain shower."
    ),
    "photos": ["https://x/1.jpg", "https://x/2.jpg", "https://x/3.jpg"],
    "photo_meta": {"https://x/1.jpg": {"caption": "The balcony"}},
}

MARKETING = {"usp": "A quiet courtyard hotel five minutes from the beach.", "best_for": ["couples"]}


def facts(**overrides):
    base = dict(property_row=HOTEL, marketing=MARKETING, room=ROOM,
                rate_paise=320_000, meal_plan="CP")
    base.update(overrides)
    return creatives.facts_from(**base)


class TestWhatTheCopyMaySay:
    def test_a_draft_is_produced_from_real_facts(self):
        drafts = creatives.generate(facts())
        assert drafts
        rate_led = next(d for d in drafts if d.template_key == "rate_led")
        assert "Garden Standard" in rate_led.headline
        assert "Anjuna" in rate_led.headline
        assert "₹3,200" in rate_led.headline

    def test_a_template_that_cannot_be_honest_does_not_run(self):
        # No price and no city — the rate-led template has nothing true to say,
        # so it produces nothing rather than something plausible.
        drafts = creatives.generate(facts(rate_paise=None, property_row={"name": "X"}))
        assert "rate_led" not in {d.template_key for d in drafts}

    def test_a_hotel_with_almost_nothing_still_gets_a_draft(self):
        # The alternative is an empty box and an instruction to write an ad,
        # which is the screen everybody closes.
        drafts = creatives.generate(
            creatives.facts_from(property_row={"name": "The Inn"}, marketing={}, room=None)
        )
        assert drafts and drafts[-1].template_key == "plain"
        assert "The Inn" in drafts[-1].headline

    def test_the_offer_template_runs_only_on_an_authorised_discount(self):
        without = {d.template_key for d in creatives.generate(facts())}
        assert "offer_led" not in without
        with_offer = {
            d.template_key for d in creatives.generate(facts(offer={"discountPct": 15}))
        }
        assert "offer_led" in with_offer

    def test_the_discount_in_the_copy_is_the_one_in_the_offer(self):
        draft = next(
            d for d in creatives.generate(facts(offer={"discountPct": 15}))
            if d.template_key == "offer_led"
        )
        assert "15%" in draft.headline or "15%" in draft.primary_text
        assert "20%" not in draft.primary_text

    def test_every_generated_draft_passes_its_own_claims_check(self):
        # The property that matters most: nothing this file writes can be
        # refused for asserting a figure the offer does not authorise.
        offer = {"discountPct": 15, "pricePaise": 320_000}
        for draft in creatives.generate(facts(offer=offer)):
            problems = creatives.check_copy(draft.headline, draft.primary_text, offer)
            assert not problems, (draft.template_key, problems)

    def test_a_hotelier_cannot_paste_in_a_discount_nobody_approved(self):
        problems = creatives.check_copy(
            "50% off this weekend", "Book direct and save.", {"discountPct": 10},
        )
        assert problems

    def test_copy_never_exceeds_metas_own_limits(self):
        long_room = dict(ROOM, name="The Extremely Long Room Name That Goes On " * 3)
        for draft in creatives.generate(facts(room=long_room)):
            assert len(draft.headline) <= creatives.MAX_HEADLINE
            assert len(draft.primary_text) <= creatives.MAX_PRIMARY


class TestTruncation:
    def test_copy_never_ends_on_a_dangling_word(self):
        # The bug this was written for: "…with a private balcony over the…"
        # in a public ad, found by generating from a real description.
        for text in (
            ROOM["description"],
            "A room with a view of the",
            "Set among the palms and the",
            "Right next to the",
        ):
            out = creatives._first_sentence(text, 70)
            if out:
                last = out.rstrip("…").rstrip(",;:-").split(" ")[-1].lower()
                assert last not in creatives._DANGLING, out

    def test_a_short_description_is_left_alone(self):
        assert creatives._first_sentence("Quiet, by the sea.", 70) == "Quiet, by the sea."

    def test_a_sentence_boundary_is_preferred_to_a_word_boundary(self):
        out = creatives._first_sentence("Quiet. And then a great deal more text " * 5, 70)
        assert out == "Quiet."

    def test_text_that_is_all_dangling_words_produces_nothing(self):
        assert creatives._first_sentence("the the the the " * 20, 70) is None


class TestMedia:
    def test_a_carousel_keeps_its_order_and_its_captions(self):
        media = creatives.build_media(
            fmt="carousel", photos=ROOM["photos"], photo_meta=ROOM["photo_meta"],
        )
        assert [c["position"] for c in media] == [0, 1, 2]
        assert media[0]["caption"] == "The balcony"
        assert media[1]["caption"] is None

    def test_a_carousel_is_capped_at_metas_limit(self):
        media = creatives.build_media(fmt="carousel", photos=[f"u{i}" for i in range(40)])
        assert len(media) == creatives.MAX_CAROUSEL_CARDS

    def test_a_single_image_takes_the_first_photo(self):
        media = creatives.build_media(fmt="single_image", photos=ROOM["photos"])
        assert media == [{"kind": "image", "url": "https://x/1.jpg"}]

    def test_an_existing_post_carries_no_media_of_its_own(self):
        # The creative is Meta's and already published; copying its URLs would
        # create a second version of an asset that can change under us.
        assert creatives.build_media(fmt="existing_post", instagram_media_id="17900") == []

    def test_a_video_carries_its_url(self):
        assert creatives.build_media(fmt="video", video_url="https://x/v.mp4") == [
            {"kind": "video", "url": "https://x/v.mp4"}
        ]


class TestFormatRefusals:
    def test_a_carousel_needs_two_photos_and_says_so_in_english(self):
        problem = creatives.format_problem("carousel", [{"url": "a"}], None)
        assert problem and "at least 2 photos" in problem

    def test_a_boost_needs_a_post_to_boost(self):
        assert creatives.format_problem("existing_post", [], None)
        assert creatives.format_problem("existing_post", [], "17900") is None

    def test_a_video_ad_needs_a_video(self):
        assert creatives.format_problem("video", [], None)

    def test_a_complete_creative_has_no_problem(self):
        assert creatives.format_problem("single_image", [{"url": "a"}], None) is None


# ── publishing ───────────────────────────────────────────────────────────────


CAMPAIGN = {
    "id": "c1", "name": "Monsoon", "channel": "instagram", "objective": "messages",
    "destination": "whatsapp", "offer": {"discountPct": 10, "pricePaise": 320_000},
    "daily_budget_paise": 50_000,
}
CREATIVE = {
    "format": "single_image", "headline": "Garden Standard in Anjuna from ₹3,200",
    "primary_text": "10% off when you book direct with us.",
    "media": [{"kind": "image", "url": "https://x/1.jpg"}],
}


class TestPublishing:
    def test_a_hotel_with_no_ad_account_is_told_which_thing_is_missing(self):
        result = run(meta_ads.publish(
            campaign=CAMPAIGN, creative=CREATIVE, audience=None, connection=None,
        ))
        assert result.status == "draft"
        assert "instagram_account" in result.missing
        assert result.ok is False

    def test_missing_credentials_is_not_reported_as_a_failure(self):
        # `failed` would send somebody looking for a Meta rejection that does
        # not exist. Nothing failed; nothing was attempted.
        result = run(meta_ads.publish(
            campaign=CAMPAIGN, creative=CREATIVE, audience=None, connection=None,
        ))
        assert result.status != "failed"

    def test_a_broken_creative_is_refused_before_credentials_are_even_checked(self):
        # A refusal a hotelier can act on beats one that is technically the
        # first thing wrong.
        result = run(meta_ads.publish(
            campaign=CAMPAIGN,
            creative={**CREATIVE, "format": "carousel", "media": [{"url": "a"}]},
            audience=None, connection=None,
        ))
        assert "at least 2 photos" in (result.detail or "")
        assert not result.missing

    def test_copy_claiming_an_unauthorised_discount_never_reaches_meta(self):
        result = run(meta_ads.publish(
            campaign=CAMPAIGN,
            creative={**CREATIVE, "primary_text": "50% off everything!"},
            audience=None, connection=None,
        ))
        assert result.status == "draft" and result.detail
        assert not result.ok

    def test_nothing_ever_returns_published_without_an_external_id(self):
        # The invariant the database also enforces, asserted here so a future
        # code path cannot be the first place it breaks.
        for connection in (None, {"status": "pending"}, {"status": "connected"}):
            result = run(meta_ads.publish(
                campaign=CAMPAIGN, creative=CREATIVE, audience=None, connection=connection,
            ))
            assert not (result.status in ("published", "running") and not result.external_id)


class TestTheMetaRequest:
    def test_a_carousel_becomes_child_attachments_in_order(self):
        spec = meta_ads.build_request(
            campaign=CAMPAIGN,
            creative={
                "format": "carousel",
                "headline": "H", "primary_text": "P",
                "media": [
                    {"url": "c", "position": 2, "caption": "third"},
                    {"url": "a", "position": 0, "caption": "first"},
                    {"url": "b", "position": 1},
                ],
            },
            audience=None, page_id=None, whatsapp_number="919000000000",
        )
        assert [c["picture"] for c in spec["creative"]["child_attachments"]] == ["a", "b", "c"]

    def test_an_ad_is_created_paused(self):
        # Never live on creation. A human presses go, on Meta, with the money
        # in front of them.
        spec = meta_ads.build_request(
            campaign=CAMPAIGN, creative=CREATIVE, audience=None,
            page_id=None, whatsapp_number="919000000000",
        )
        assert spec["status"] == "PAUSED"

    def test_a_whatsapp_destination_opens_a_thread_rather_than_a_page(self):
        spec = meta_ads.build_request(
            campaign=CAMPAIGN, creative=CREATIVE, audience=None,
            page_id="page1", whatsapp_number="919000000000",
        )
        assert spec["destination"] == {"type": "whatsapp", "number": "919000000000"}

    def test_interests_are_marked_unresolved_rather_than_passed_as_ids(self):
        # They are the hotelier's words. Sending them where Meta expects
        # interest ids would target the ad at something arbitrary.
        spec = meta_ads.build_request(
            campaign=CAMPAIGN, creative=CREATIVE,
            audience={"interests": ["Travel", "Couples"], "age_min": 25},
            page_id=None, whatsapp_number=None,
        )
        assert spec["targeting"]["interests_unresolved"] == ["Travel", "Couples"]
        assert "interests" not in spec["targeting"]

    def test_every_destination_maps_to_a_meta_cta(self):
        for destination in creatives.CTAS:
            assert destination in meta_ads.META_CTA

    def test_every_objective_maps_to_a_meta_objective(self):
        for objective in ("messages", "traffic", "awareness", "engagement", "leads"):
            assert objective in meta_ads.META_OBJECTIVE
