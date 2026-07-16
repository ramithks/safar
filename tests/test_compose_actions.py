"""Unified composer + structured actions — pure units (no DB, no network)."""
import pytest
from fastapi import HTTPException

from app.api.v1.plan_trip.models import PlanRequest
from app.trip_state.compose import allocate_nights, build_skeleton


class TestSpecModels:
    def test_stops_coerce_strings_and_objects(self):
        r = PlanRequest(origin="Bhiwani", destination="Udaipur",
                        stops=["Jaipur", {"city": "Pushkar", "nights": 2}])
        assert [s.city for s in r.stops] == ["Jaipur", "Pushkar"]
        assert r.stops[0].nights is None and r.stops[1].nights == 2

    def test_duplicate_stop_rejected(self):
        with pytest.raises(ValueError, match="Duplicate"):
            PlanRequest(origin="A B", destination="Udaipur", stops=["udaipur"])
        with pytest.raises(ValueError, match="Duplicate"):
            PlanRequest(origin="Delhi", destination="Goa", stops=["Pune", "pune"])

    def test_max_four_stops(self):
        with pytest.raises(ValueError, match="At most 4"):
            PlanRequest(origin="A B", destination="Z Z",
                        stops=["C1", "C2", "C3", "C4", "C5"])

    def test_zero_stops_default(self):
        assert PlanRequest(origin="Delhi", destination="Goa").stops == []


class TestNightsAllocation:
    def test_terminal_absorbs_remainder(self):
        assert allocate_nights(6, [{"city": "Jaipur"}, {"city": "Pushkar", "nights": 2}]) == ([1, 2], 2)

    def test_zero_stops_all_terminal(self):
        assert allocate_nights(4, []) == ([], 3)

    def test_terminal_never_below_one(self):
        stops = [{"city": "A", "nights": 5}, {"city": "B", "nights": 5}]
        nights, terminal = allocate_nights(3, stops)
        assert nights == [5, 5] and terminal == 1


class TestSkeleton:
    def test_route_shape(self):
        g = build_skeleton({"origin": "Bhiwani", "destination": "Udaipur", "days": 6,
                            "stops": [{"city": "Jaipur"}, {"city": "Pushkar", "nights": 2}]})
        assert [(n.city, n.role) for n in g.nodes] == [
            ("Bhiwani", "origin"), ("Jaipur", "stop"), ("Pushkar", "stop"), ("Udaipur", "terminal")]
        assert [(e.from_city, e.to_city) for e in g.edges] == [
            ("Bhiwani", "Jaipur"), ("Jaipur", "Pushkar"),
            ("Pushkar", "Udaipur"), ("Udaipur", "Bhiwani")]
        assert all(e.routes == [] for e in g.edges)

    def test_zero_stops_is_same_pipeline(self):
        g = build_skeleton({"origin": "Delhi", "destination": "Goa", "days": 4, "stops": []})
        assert [(e.from_city, e.to_city) for e in g.edges] == [("Delhi", "Goa"), ("Goa", "Delhi")]
        assert g.nodes[-1].nights == 3

    def test_deterministic(self):
        spec = {"origin": "A B", "destination": "C D", "days": 5, "stops": [{"city": "X Y"}]}
        assert build_skeleton(spec).model_dump() == build_skeleton(spec).model_dump()


# Selection carry-forward is DOC-level now — tested in tests/test_trip_doc.py.


class TestActionSpecMutation:
    def _spec(self):
        return {"origin": "Bhiwani", "destination": "Udaipur", "days": 5,
                "travelers": 2, "budget_tier": "mid", "interests": [],
                "stops": [{"city": "Jaipur", "nights": 1}], "version": 3}

    def _mutate(self, spec, **kw):
        from app.api.v3.trips import ActionBody, _mutate_spec
        return _mutate_spec(spec, ActionBody(**kw))

    def test_add_stop(self):
        spec = self._spec()
        note = self._mutate(spec, type="add_stop", city="Pushkar", nights=2)
        assert "Pushkar" in note
        assert spec["stops"][-1] == {"city": "Pushkar", "nights": 2}

    def test_add_duplicate_stop_rejected(self):
        with pytest.raises(HTTPException) as e:
            self._mutate(self._spec(), type="add_stop", city="jaipur")
        assert e.value.status_code == 400

    def test_remove_stop(self):
        spec = self._spec()
        self._mutate(spec, type="remove_stop", city="JAIPUR")
        assert spec["stops"] == []

    def test_remove_unknown_stop_rejected(self):
        with pytest.raises(HTTPException):
            self._mutate(self._spec(), type="remove_stop", city="Goa")

    def test_change_budget_validated(self):
        spec = self._spec()
        self._mutate(spec, type="change_budget", budget="high")
        assert spec["budget_tier"] == "high"
        with pytest.raises(HTTPException):
            self._mutate(spec, type="change_budget", budget="luxe")

    def test_change_dates_rejects_past(self):
        with pytest.raises(HTTPException):
            self._mutate(self._spec(), type="change_dates", departure_date="2020-01-01")

