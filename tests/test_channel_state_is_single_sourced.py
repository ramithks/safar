"""
One answer to "is Instagram connected?", wherever it is asked.

**The bug this file exists for.** The Settings screen showed "Connect
Instagram" while the Channels screen — one click away — showed "Connected", for
the same hotel, at the same moment, and refreshing changed nothing. It was not
a caching problem. Settings queried *nothing*: it rendered a connect button
unconditionally, so the two screens could not agree by construction, and no
amount of invalidation would have fixed it because there was no second state to
invalidate.

The repair is that both clients read one server-decided row through one cached
query. `instagram_row` is the only function that turns a connection into words,
and this file asserts that every state a connection can be in has exactly one
defined answer — because "we handled connected and not-connected" is precisely
how the original bug looked from outside.
"""
import pytest

from app.api.v1.messaging.oauth import instagram_row

# Every state the row can hold. A connection can be missing entirely (nobody has
# ever connected), connected, connected-but-sandboxed, deliberately
# disconnected, or in an error state Meta or a token expiry put it in.
STATES = [
    None,
    {"status": "connected", "username": "harryhotel", "is_sandbox": False},
    {"status": "connected", "username": "harryhotel", "is_sandbox": True},
    {"status": "disconnected", "username": "harryhotel", "last_error": None},
    {"status": "error", "username": "harryhotel", "last_error": "Token expired."},
    {"status": "revoked", "username": "harryhotel", "last_error": None},
    {"status": None, "username": None},
]


@pytest.mark.parametrize("connection", STATES)
def test_every_state_a_connection_can_be_in_has_one_defined_answer(connection):
    """
    No state falls through to a blank row. The screens render `state`,
    `headline`, `detail` and `action` directly, so an undefined one is an empty
    card on a hotel's settings page.
    """
    row = instagram_row(connection)
    assert row.id == "instagram"
    assert row.state in ("connected", "action_needed", "not_connected")
    assert row.headline.strip()
    assert row.detail.strip()
    assert row.action and row.action.strip()


def test_a_connected_account_says_connected_and_offers_the_way_out():
    row = instagram_row({"status": "connected", "username": "harryhotel",
                         "is_sandbox": False})
    assert row.state == "connected"
    assert row.headline == "@harryhotel"
    assert row.action == "Disconnect"


def test_a_deliberate_disconnect_is_not_an_error():
    """
    `disconnected` used to be read as "needs reconnecting", which put an amber
    "Needs you" badge on a channel the hotel had switched off on purpose — the
    product telling an owner their setup is broken because they used it.
    """
    row = instagram_row({"status": "disconnected", "username": "harryhotel",
                         "last_error": None})
    assert row.state == "not_connected"
    assert "Connect" in (row.action or "")
    # And it reads as "you turned this off", not as "you never set this up".
    assert "stopped answering" in row.detail


def test_a_real_failure_still_asks_to_be_reconnected():
    row = instagram_row({"status": "error", "username": "h",
                         "last_error": "Token expired."})
    assert row.state == "action_needed"
    assert row.action == "Reconnect"
    # The server's own words reach the screen; no client maps a status to prose.
    assert row.detail == "Token expired."


def test_a_sandbox_connection_is_not_told_to_the_hotel_as_connected():
    """
    A sandbox row consumes no real consent and no guest message reaches it.
    Reporting it as connected would tell a hotel its Instagram is being
    answered when nothing is.
    """
    assert instagram_row(
        {"status": "connected", "username": "h", "is_sandbox": True},
    ).state == "not_connected"


def test_the_words_come_from_here_and_only_here():
    """
    Every string the two screens render is on this row. The moment a client
    starts deriving one of them from `state`, there are two vocabularies again
    and they drift the first time either is edited.
    """
    for connection in STATES:
        row = instagram_row(connection)
        assert row.headline != row.state
        assert not row.detail.startswith(str(row.state))
