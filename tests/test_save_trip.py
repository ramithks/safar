"""
Tests for POST /api/v1/save-trip endpoint.
Run with: pytest tests/test_save_trip.py -v
"""
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from app.main import app
from app.deps.auth import require_user


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_user(user_id="user-abc"):
    user = MagicMock()
    user.id = user_id
    return user


def _make_db(trip_insert_data=None, pref_existing_data="NONE"):
    """
    Build a per-table Supabase client mock.

    pref_existing_data:
        "NONE"  (default) → maybe_single().execute() returns None  (new user, HTTP 406)
        None             → row found but .data is None
        dict             → row found with .data = dict
    """
    trips_mock = MagicMock()
    trip_res = MagicMock()
    trip_res.data = trip_insert_data if trip_insert_data is not None else [{"id": "trip-uuid-123"}]
    trips_mock.insert.return_value.execute.return_value = trip_res

    items_mock = MagicMock()
    items_res = MagicMock()
    items_res.data = []
    items_mock.insert.return_value.execute.return_value = items_res

    pref_mock = MagicMock()
    if pref_existing_data == "NONE":
        # maybe_single returns None → new user with no pref row
        pref_mock.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = None
    else:
        pref_res = MagicMock()
        pref_res.data = pref_existing_data
        pref_mock.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = pref_res
    pref_mock.upsert.return_value.execute.return_value = MagicMock()

    table_map = {
        "trips": trips_mock,
        "itinerary_items": items_mock,
        "user_preference_graph": pref_mock,
    }

    db = MagicMock()
    db.table.side_effect = lambda name: table_map[name]
    return db, trips_mock, items_mock, pref_mock


MINIMAL_TRIP = {
    "tripData": {
        "trip_name": "Paris Escape",
        "destination": "Paris",
        "currency": "EUR",
        "days": [
            {
                "day": 1,
                "activities": [
                    {
                        "title": "Eiffel Tower",
                        "lat": 48.8584,
                        "lng": 2.2945,
                        "time": "10:00",
                        "estimated_cost": 25,
                        "type": "attraction",
                        "description": "Visit the tower",
                        "location": "Champ de Mars",
                    }
                ],
            }
        ],
    },
    "destinationName": "Paris",
    "is_public": True,
}


@pytest.fixture(autouse=True)
def override_auth():
    """Replace require_user with a stub that returns a fake user for all tests."""
    fake = _fake_user()
    app.dependency_overrides[require_user] = lambda: fake
    yield fake
    app.dependency_overrides.pop(require_user, None)


@pytest.fixture()
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSaveTripSuccess:
    def test_new_user_no_pref_graph_row(self, client):
        """First-ever save: maybe_single returns None — must not raise AttributeError."""
        db, _, _, _ = _make_db(pref_existing_data="NONE")

        with patch("app.api.v1.save_trip.get_db", return_value=db):
            resp = client.post("/api/v1/save-trip", json=MINIMAL_TRIP)

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["tripId"] == "trip-uuid-123"

    def test_existing_user_pref_row_updated(self, client):
        """User already has a pref row — affinity counter increments."""
        db, _, _, pref_mock = _make_db(
            pref_existing_data={"total_trips": 3, "destination_affinity": {"Paris": 20}}
        )

        with patch("app.api.v1.save_trip.get_db", return_value=db):
            resp = client.post("/api/v1/save-trip", json=MINIMAL_TRIP)

        assert resp.status_code == 200
        upsert_call = pref_mock.upsert.call_args[0][0]
        assert upsert_call["total_trips"] == 4
        assert upsert_call["destination_affinity"]["Paris"] == 30

    def test_activities_without_coords_skipped(self, client):
        """Activities missing lat/lng are not inserted into itinerary_items."""
        trip_data = {
            "tripData": {
                "trip_name": "No Coords Trip",
                "destination": "Paris",
                "days": [{"day": 1, "activities": [{"title": "Secret Spot"}]}],
            },
            "destinationName": "Paris",
            "is_public": False,
        }
        db, _, items_mock, _ = _make_db(pref_existing_data="NONE")

        with patch("app.api.v1.save_trip.get_db", return_value=db):
            resp = client.post("/api/v1/save-trip", json=trip_data)

        assert resp.status_code == 200
        items_mock.insert.assert_not_called()

    def test_empty_days_list(self, client):
        """Trip with no days should still save without error."""
        trip_data = {
            "tripData": {"trip_name": "Empty", "days": []},
            "destinationName": "Nowhere",
            "is_public": True,
        }
        db, _, _, _ = _make_db(pref_existing_data="NONE")

        with patch("app.api.v1.save_trip.get_db", return_value=db):
            resp = client.post("/api/v1/save-trip", json=trip_data)

        assert resp.status_code == 200

    def test_budget_computed_from_activities(self, client):
        """Total cost is summed across all activities."""
        db, trips_mock, _, _ = _make_db(pref_existing_data="NONE")

        with patch("app.api.v1.save_trip.get_db", return_value=db):
            resp = client.post("/api/v1/save-trip", json=MINIMAL_TRIP)

        assert resp.status_code == 200
        insert_call = trips_mock.insert.call_args[0][0]
        assert insert_call["budget"] == 25

    def test_new_user_pref_graph_upserted_with_correct_values(self, client):
        """New user gets total_trips=1 and fresh destination affinity."""
        db, _, _, pref_mock = _make_db(pref_existing_data="NONE")

        with patch("app.api.v1.save_trip.get_db", return_value=db):
            client.post("/api/v1/save-trip", json=MINIMAL_TRIP)

        upsert_call = pref_mock.upsert.call_args[0][0]
        assert upsert_call["total_trips"] == 1
        assert upsert_call["destination_affinity"]["Paris"] == 10
        assert upsert_call["user_id"] == "user-abc"

    def test_multiple_activities_cost_summed(self, client):
        """Budget accumulates across multiple activities on multiple days."""
        trip_data = {
            "tripData": {
                "days": [
                    {
                        "day": 1,
                        "activities": [
                            {"title": "A", "lat": 1.0, "lng": 1.0, "estimated_cost": 10},
                            {"title": "B", "lat": 2.0, "lng": 2.0, "estimated_cost": 20},
                        ],
                    },
                    {
                        "day": 2,
                        "activities": [
                            {"title": "C", "lat": 3.0, "lng": 3.0, "estimated_cost": 30},
                        ],
                    },
                ]
            },
            "destinationName": "Tokyo",
            "is_public": True,
        }
        db, trips_mock, _, _ = _make_db(pref_existing_data="NONE")

        with patch("app.api.v1.save_trip.get_db", return_value=db):
            resp = client.post("/api/v1/save-trip", json=trip_data)

        assert resp.status_code == 200
        insert_call = trips_mock.insert.call_args[0][0]
        assert insert_call["budget"] == 60


class TestSaveTripFailure:
    def test_db_insert_returns_empty_data_raises_500(self):
        """If Supabase trips insert returns no data, endpoint must return 500."""
        db, _, _, _ = _make_db(trip_insert_data=[])  # empty list → falsy

        with patch("app.api.v1.save_trip.get_db", return_value=db):
            resp = TestClient(app, raise_server_exceptions=False).post(
                "/api/v1/save-trip", json=MINIMAL_TRIP
            )

        assert resp.status_code == 500
        assert "Failed to save trip" in resp.json()["detail"]
