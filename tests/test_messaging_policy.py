"""
Every channel a conversation can carry has to be answerable.

This file exists because the same bug shipped twice.

`list_conversations` asks `window_open()` about every row in a hotel's inbox.
`for_channel` raises on a channel it does not know. So one conversation on an
unhandled channel does not degrade that conversation — it 500s the **whole
Inbox screen**, and the hotel cannot read any of its guest messages.

    internal   the staff console.  Fixed, with a comment explaining it.
    website    the chat on the hotel's own booking page. Added afterwards,
               and broke the same screen in the same way — because the fix
               had been to that one channel rather than to the rule.

The rule is: a channel the database will store is a channel this module has to
have an answer for. The database's own CHECK constraint is the list, which is
why the constant below is a copy of it rather than a hand-written set — a
migration that adds a channel and forgets the policy fails here, at the point
where somebody can still do something about it.
"""
import pytest

from app.services.messaging import policy


# Copied from the CHECK on `conversations.channel`. Keep in step: the point of
# this list is to be the same list.
#
#   check (channel = any (array['whatsapp','instagram','website','phone',
#                               'email','internal']))
STORABLE_CHANNELS = ("whatsapp", "instagram", "website", "phone", "email", "internal")


@pytest.mark.parametrize("channel", STORABLE_CHANNELS)
def test_every_storable_channel_has_an_answer(channel):
    """
    Either a policy or an explicit "no window". Never an exception.

    This is the assertion that would have caught the website bug the day the
    website chat was written, instead of a hotel's Inbox catching it.
    """
    if channel in policy.WINDOWLESS:
        assert policy.has_window(channel) is False
        return
    # A channel with a policy must produce one without raising.
    assert policy.for_channel(channel) is not None, channel


@pytest.mark.parametrize("channel", STORABLE_CHANNELS)
def test_asking_whether_the_window_is_open_never_raises(channel):
    """
    The exact call `list_conversations` makes on every inbox row.

    A boolean, always. The Inbox is the screen a hotel opens to answer guests,
    and it must not be possible for one guest's choice of channel to take it
    down.
    """
    assert isinstance(policy.has_window(channel), bool)


def test_the_website_chat_carries_no_window():
    """
    Not an oversight — there is no rule to inherit.

    A 24-hour window exists because Meta bills by one. A guest on a hotel's own
    booking page can be replied to while the page is open, and after that there
    is nobody to deliver to at all. Giving it WhatsApp's window would be
    inventing a constraint Meta never applied to a page we serve ourselves.
    """
    assert "website" in policy.WINDOWLESS
    assert policy.has_window("website") is False


def test_a_channel_nobody_has_thought_about_still_raises():
    """
    The original rule, and it stays.

    An unknown channel must not silently inherit WhatsApp's window and template
    behaviour — that is how a message goes out under rules Meta never agreed
    to. Unknown is an error; *known and windowless* is the thing that needed a
    way to be said.
    """
    with pytest.raises(ValueError):
        policy.for_channel("telegram")
    assert policy.has_window("telegram") is True


def test_the_billing_channels_and_the_policy_channels_agree():
    """
    A guest channel that can be charged for is one a conversation can be on.

    `billing.USAGE_BY_GUEST_CHANNEL` prices website, Instagram and WhatsApp.
    If one of those were not storable, the charge would be for a conversation
    that could never exist — and the mismatch would show up as revenue nobody
    could explain.
    """
    from app.services import billing

    for channel in billing.USAGE_BY_GUEST_CHANNEL:
        assert channel in STORABLE_CHANNELS, channel
