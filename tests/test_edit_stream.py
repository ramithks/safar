"""
Streaming copilot edit endpoint — verifies the SSE framing: reply arrives as
`token` chunks and a terminal `done` event carries the authoritative patches +
intent + clarify options. apply_edit itself is unit-tested elsewhere, so here
it's stubbed to a fixed result and we assert the transport shape.
"""
import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.deps.auth import require_user
from app.api.v1.trip_state import endpoint as ep


def _fake_user(user_id="user-abc"):
    user = MagicMock()
    user.id = user_id
    return user


@pytest.fixture(autouse=True)
def override_auth():
    fake = _fake_user()
    app.dependency_overrides[require_user] = lambda: fake
    yield fake
    app.dependency_overrides.pop(require_user, None)


def _parse_sse(text: str):
    events = []
    for frame in text.split("\n\n"):
        if not frame.strip():
            continue
        ev = next((l[6:].strip() for l in frame.splitlines() if l.startswith("event:")), None)
        data = next((l[5:].strip() for l in frame.splitlines() if l.startswith("data:")), None)
        events.append((ev, json.loads(data) if data else None))
    return events


def test_edit_stream_emits_tokens_then_done(monkeypatch):
    # Authorized, existing trip.
    monkeypatch.setattr(
        ep.store, "get_trip_state",
        lambda trip_id: {"id": trip_id, "user_id": "user-abc", "visibility": "private", "state": {}},
    )

    async def fake_apply_edit(trip_id, message, state, action=None, user_id=""):
        return {
            "reply": "Locked in the cheaper bus.",
            "intent": "select_transport_option",
            "applied": True,
            "patches": [{"op": "set", "path": "/costs", "value": {"total": 100}}],
            "clarify_options": [],
        }

    monkeypatch.setattr(ep, "apply_edit", fake_apply_edit)

    tid = "5e9e3cce-9647-43d2-bacf-60480509e080"
    with TestClient(app) as client:
        res = client.post(f"/api/v1/trip-state/{tid}/edit/stream", json={"message": "cheaper bus"})
    assert res.status_code == 200
    assert "text/event-stream" in res.headers["content-type"]

    events = _parse_sse(res.text)
    tokens = [d["text"] for ev, d in events if ev == "token"]
    assert "".join(tokens) == "Locked in the cheaper bus."

    done = next(d for ev, d in events if ev == "done")
    assert done["intent"] == "select_transport_option"
    assert done["applied"] is True
    assert done["patches"][0]["path"] == "/costs"
    assert done["clarify_options"] == []


def test_edit_stream_clarify_carries_options(monkeypatch):
    monkeypatch.setattr(
        ep.store, "get_trip_state",
        lambda trip_id: {"id": trip_id, "user_id": "user-abc", "visibility": "private", "state": {}},
    )

    async def fake_apply_edit(trip_id, message, state, action=None, user_id=""):
        return {
            "reply": "How many days would you like in Goa?",
            "intent": "clarify",
            "applied": False,
            "patches": [],
            "clarify_options": ["1 day", "2 days", "3 days"],
        }

    monkeypatch.setattr(ep, "apply_edit", fake_apply_edit)

    tid = "5e9e3cce-9647-43d2-bacf-60480509e080"
    with TestClient(app) as client:
        res = client.post(f"/api/v1/trip-state/{tid}/edit/stream", json={"message": "add Goa"})
    done = next(d for ev, d in _parse_sse(res.text) if ev == "done")
    assert done["intent"] == "clarify"
    assert done["clarify_options"] == ["1 day", "2 days", "3 days"]
    assert done["applied"] is False
