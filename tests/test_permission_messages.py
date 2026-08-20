"""
What a refusal says to the person who is refused.

These are strings a receptionist reads on their own phone, so they are pinned
like any other piece of product copy. The message this file exists to prevent
is the one it replaced: "Your role (receptionist) cannot agents." -- which
leaks an internal permission name, is not a sentence, and tells the person
nothing about what to do next.
"""
import pytest
from fastapi import HTTPException

from app.deps.property import PERM_VERB, ROLE_PERMS, Member


def member(role: str) -> Member:
    return Member(
        profile_id="p", member_id="m", partner_id="pa", property_id="pr",
        role=role, display_name="Someone", extra_perms=frozenset(),
    )


def test_the_refusal_is_a_sentence_a_receptionist_can_read():
    with pytest.raises(HTTPException) as exc:
        member("receptionist").require("agents")
    detail = exc.value.detail
    assert detail == "Receptionists can't set up the AI employee. Ask the owner or a manager."


def test_it_never_leaks_the_internal_permission_name():
    for role in ("receptionist", "housekeeping", "driver"):
        for perm in ("agents", "rates", "folio", "marketing"):
            if member(role).can(perm):
                continue
            with pytest.raises(HTTPException) as exc:
                member(role).require(perm)
            detail = exc.value.detail
            # The bare slug must not appear -- "rates" is a real English word,
            # so the check is that the *phrasing* replaced it.
            assert PERM_VERB[perm] in detail
            assert not detail.startswith("Your role")
            assert detail.endswith("Ask the owner or a manager.")


def test_every_permission_a_role_can_be_refused_has_a_phrasing():
    """A missing entry silently falls back to the slug, which is the bug."""
    every_perm = {p for perms in ROLE_PERMS.values() for p in perms if p != "*"}
    assert every_perm <= set(PERM_VERB), f"no phrasing for {every_perm - set(PERM_VERB)}"


def test_an_owner_is_never_refused():
    member("owner").require("agents")
    member("owner").require("billing")
