"""Integrity layer — checksums, manifests, copilot gating (pure units, no DB)."""
import pytest
from fastapi import HTTPException

from app.trip_state import integrity


def _state():
    return {
        "spec": {"origin": "bhiwani", "destination": "Udaipur", "stops": [], "version": 1},
        "request": {"origin": "bhiwani", "destination": "Udaipur"},
        "graph": {
            "nodes": [
                {"id": "stop-1", "city": "Udaipur",
                 "stay": {"search_results": [{"name": "Lake Palace", "result_id": "r-hotel-1"}]}},
            ],
            "edges": [
                {"id": "leg-1", "from_city": "bhiwani", "to_city": "Udaipur",
                 "routes": [{"id": "rt", "hops": [{"options": [
                     {"id": "train-0", "mode": "train", "result_id": "r-train-9"},
                     {"id": "train-1", "mode": "train"},          # no result_id (legacy)
                 ]}]}]},
            ],
        },
        "meta": {"status": "ready"},
    }


class TestChecksums:
    def test_stamp_then_verify_ok(self):
        state = _state()
        integrity.stamp(state)
        assert state["meta"]["checksums"]["algo"] == integrity.CHECKSUM_ALGO
        assert integrity.verify(state) == "ok"

    def test_deterministic_across_key_order(self):
        a, b = _state(), _state()
        # Same content, different insertion order for a nested dict.
        b["graph"]["edges"][0] = dict(reversed(list(b["graph"]["edges"][0].items())))
        ca = integrity.compute_checksums(a)
        cb = integrity.compute_checksums(b)
        assert ca["graph"] == cb["graph"]
        assert ca["spec"] == cb["spec"]
        assert ca["search"] == cb["search"]

    def test_graph_tamper_detected(self):
        state = _state()
        integrity.stamp(state)
        state["graph"]["edges"][0]["to_city"] = "Jaipur"      # bit flip after seal
        assert integrity.verify(state) == "corrupted"

    def test_spec_tamper_detected(self):
        state = _state()
        integrity.stamp(state)
        state["spec"]["days"] = 99
        assert integrity.verify(state) == "corrupted"

    def test_search_refs_tamper_detected(self):
        state = _state()
        integrity.stamp(state)
        state["graph"]["edges"][0]["routes"][0]["hops"][0]["options"][0]["result_id"] = "r-other"
        assert integrity.verify(state) == "corrupted"

    def test_unsealed_legacy_state(self):
        assert integrity.verify(_state()) == "unsealed"
        assert integrity.verify({}) == "unsealed"

    def test_restamp_after_legit_change_is_ok(self):
        state = _state()
        integrity.stamp(state)
        state["graph"]["edges"][0]["selection"] = {"route_id": "rt", "per_hop_option_ids": ["train-0"]}
        integrity.stamp(state)                                 # what the store does on commit
        assert integrity.verify(state) == "ok"

    def test_referenced_ids_sorted_and_deduped(self):
        ids = integrity._referenced_result_ids(_state()["graph"])
        assert ids == ["r-hotel-1", "r-train-9"]

    def test_spec_falls_back_to_request(self):
        state = _state()
        del state["spec"]
        integrity.stamp(state)
        assert integrity.verify(state) == "ok"
        state["request"]["origin"] = "delhi"
        assert integrity.verify(state) == "corrupted"


class TestManifest:
    def test_build_and_finalize(self):
        m = integrity.build_manifest(spec_version=3)
        assert m["spec_version"] == 3
        assert m["status"] == "running"
        assert m["route_model"] and m["itinerary_model"]
        assert set(m["provider_versions"]) == {"flight", "hotel", "train", "bus"}
        assert m["started_at"] and m["completed_at"] is None

        integrity.finalize_manifest(m)
        assert m["status"] == "complete" and m["completed_at"]

    def test_finalize_failed(self):
        m = integrity.build_manifest()
        integrity.finalize_manifest(m, status="failed")
        assert m["status"] == "failed"

    def test_generation_ids_unique(self):
        assert integrity.build_manifest()["generation_id"] != integrity.build_manifest()["generation_id"]


class TestCopilotGate:
    def _gate(self, monkeypatch, enabled: bool):
        from app.config import settings
        monkeypatch.setattr(settings, "copilot_enabled", enabled)

    def test_free_text_blocked_when_disabled(self, monkeypatch):
        from app.api.v1.trip_state.endpoint import EditRequest, _require_copilot_or_action
        self._gate(monkeypatch, False)
        with pytest.raises(HTTPException) as exc:
            _require_copilot_or_action(EditRequest(message="add a stop in Jaipur"))
        assert exc.value.status_code == 403

    def test_structured_action_always_allowed(self, monkeypatch):
        from app.api.v1.trip_state.endpoint import EditAction, EditRequest, _require_copilot_or_action
        self._gate(monkeypatch, False)
        _require_copilot_or_action(EditRequest(
            action=EditAction(type="select_leg_option", leg_id="leg-1", mode="train", index=0),
        ))  # must not raise

    def test_free_text_allowed_when_enabled(self, monkeypatch):
        from app.api.v1.trip_state.endpoint import EditRequest, _require_copilot_or_action
        self._gate(monkeypatch, True)
        _require_copilot_or_action(EditRequest(message="add a stop"))  # must not raise


class TestStoreStamping:
    def test_create_state_is_sealed(self):
        """empty_trip_state → the exact dict create_trip_state persists is sealed."""
        from app.trip_state import empty_trip_state
        state = empty_trip_state({"origin": "a", "destination": "b"}).model_dump(mode="json")
        integrity.stamp(state)
        assert integrity.verify(state) == "ok"
        assert state["spec"]["version"] == 1
        assert state["spec"]["stops"] == []

    def test_meta_roundtrip_preserves_seals(self):
        """TripMeta now carries checksums/generation — model validation must not drop them."""
        from app.trip_state.models import TripState
        state = _state()
        integrity.stamp(state)
        state["meta"]["generation"] = integrity.build_manifest()
        revalidated = TripState.model_validate({**state, "meta": state["meta"]}).model_dump(mode="json")
        assert revalidated["meta"]["checksums"] == state["meta"]["checksums"]
        assert revalidated["meta"]["generation"]["generation_id"] == state["meta"]["generation"]["generation_id"]
