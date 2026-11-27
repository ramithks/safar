"""
A save that says it saved must have saved.

`website_roundtrip_e2e` sent `cover_image` on its first run, because that is a
plausible name for the field actually called `heroImageUrl`. It got a 200, an
unchanged draft, and nothing anywhere saying a word had been dropped. The suite
reported it as a finding rather than fixing it, on the grounds that tightening
the endpoint could reject a client that had been sending a stray field for
months.

§16 asks for that decision to be made rather than deferred, and to be made after
finding every caller. Every caller *in this repository* — `booking_engine_e2e`,
`domain_e2e`, `website_roundtrip_e2e` — sends only documented fields. The
builder does not live in this repository, so nothing here can prove the same of
it, and a 422 to a hotelier mid-edit is a worse bug than the one being fixed.

So: **capture, report, log, and keep the switch.** What is saved does not change
— the draft is still built from the declared fields, and a typo is still stored
nowhere, which is the correct behaviour and always was. What changes is that the
caller is told. `website_strict_fields` turns the same information into a 422
when the log has been quiet long enough, without another schema change.

What must not regress, and is asserted hardest here: a save containing a
*correct* field alongside an unknown one still saves the correct one.
"""
import pytest
from pydantic import ValidationError

from app.api.v1.pms import schemas


def request_with(**kwargs) -> schemas.WebsiteUpdateRequest:
    return schemas.WebsiteUpdateRequest(**kwargs)


# ── what the schema now does with a name it does not know ───────────────────

def test_the_key_the_suite_actually_sent_is_captured_rather_than_dropped():
    body = request_with(cover_image="https://images.staysuite.test/ignored.jpg")
    assert body.model_extra == {
        "cover_image": "https://images.staysuite.test/ignored.jpg"}
    assert sorted(body.model_extra) == ["cover_image"]


def test_a_correct_save_reports_nothing():
    body = request_with(headline="Two nights in Anjuna", accentColor="#0F766E")
    assert not body.model_extra
    assert body.headline == "Two nights in Anjuna"


def test_an_unknown_field_never_reaches_the_draft():
    """
    **The property that must not change.** A typo stored as content is worse
    than a typo ignored: it would go into `draft` and out again on every read,
    and the builder would render a key nobody defined.

    This is the same behaviour the endpoint had before — the fix is about
    telling the caller, not about storing more.
    """
    body = request_with(headline="Anjuna", cover_image="https://x/y.jpg")
    draft = body.model_dump(
        exclude_none=True,
        exclude={"template", *(body.model_extra or {})},
    )
    assert draft["headline"] == "Anjuna"
    assert "cover_image" not in draft


def test_a_good_field_beside_a_bad_one_still_saves():
    """
    The regression that permissiveness exists to prevent, from the other side.
    A builder sending one stray key must not lose the nine correct ones with it.
    """
    body = request_with(
        headline="Anjuna", tagline="Garden rooms", nonsense="x", alsoNonsense=1,
    )
    draft = body.model_dump(
        exclude_none=True,
        exclude={"template", *(body.model_extra or {})},
    )
    assert draft == {"headline": "Anjuna", "tagline": "Garden rooms"}
    assert sorted(body.model_extra) == ["alsoNonsense", "nonsense"]


# ── the declared fields still behave ────────────────────────────────────────

def test_allowing_extras_did_not_stop_the_real_validation():
    """
    `extra="allow"` is about *unknown* names. A known name with a wrong value
    must still be refused — otherwise this would have traded a silent drop for a
    silent 2,000-character `about`.
    """
    with pytest.raises(ValidationError):
        request_with(headline="x" * 201)
    with pytest.raises(ValidationError):
        request_with(about="y" * 2001)


def test_the_gallery_still_takes_both_shapes():
    """
    Older rows hold bare URL strings and the builder sends objects. Both are
    declared, so neither may land in `model_extra`.
    """
    body = request_with(gallery=[
        "https://images.staysuite.test/old.jpg",
        {"url": "https://images.staysuite.test/new.jpg", "category": "pool"},
    ])
    assert not body.model_extra
    assert len(body.gallery) == 2


# ── the response, and the switch ────────────────────────────────────────────

def test_the_response_can_carry_the_names_that_were_ignored():
    out = schemas.WebsiteConfigOut(
        propertyId="p-1", template="boutique", status="draft",
        ignoredFields=["cover_image"],
    )
    assert out.ignoredFields == ["cover_image"]


def test_a_read_reports_nothing_ignored():
    """
    Empty on every path that is not a save, so a client can render it without
    first working out which call it made.
    """
    out = schemas.WebsiteConfigOut(
        propertyId="p-1", template="boutique", status="published")
    assert out.ignoredFields == []


def test_strictness_is_off_by_default_and_is_a_setting():
    """
    The migration path §16 asks for, as a fact rather than a plan. Off, because
    the builder is in another repository and cannot be audited from here; a
    setting, so turning it on is a deploy rather than a code change.
    """
    from app.config import settings

    assert hasattr(settings, "website_strict_fields")
    assert settings.website_strict_fields is False


def test_the_endpoint_logs_and_reports_before_it_ever_refuses():
    """
    Read out of the handler, because the order matters: the log line is the
    telemetry that decides when the switch may be flipped, and a version that
    raised first would never produce it.
    """
    import ast
    import inspect

    from app.api.v1.pms import endpoint

    source = inspect.getsource(endpoint.update_website)
    tree = ast.parse(inspect.cleandoc(source))
    body = ast.unparse(tree)

    assert "model_extra" in body
    assert "website-draft" in body, "no telemetry to decide the migration on"
    assert body.index("logger.warning") < body.index("website_strict_fields"), (
        "the endpoint refuses before it logs, so the log that is supposed to "
        "tell us when refusing is safe would never be written"
    )
