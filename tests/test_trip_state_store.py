"""
Unit tests for trip-state persistence crash safety (app/trip_state/store).

Simulates the 2026-07-11 production failure modes with a fake Supabase client:
an event INSERT that commits while its response is lost in transport used to
leave `last_seq` behind the event log, so the next writer recomputed the same
seq → duplicate-key 23505, and the snapshot lost the committed event's patches.
"""
import copy

import httpx
import pytest
from postgrest.exceptions import APIError

import app.trip_state.store as store
from app.trip_state.patch import PatchOp


# ── Fake Supabase client ─────────────────────────────────────────────────────

class _Res:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, db, name):
        self.db, self.name = db, name
        self.op = None
        self.payload = None
        self.eqs = {}
        self.gts = {}

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def select(self, _cols):
        self.op = "select"
        return self

    def eq(self, col, val):
        self.eqs[col] = val
        return self

    def gt(self, col, val):
        self.gts[col] = val
        return self

    def order(self, _col):
        return self

    def limit(self, _n):
        return self

    def execute(self):
        return self.db.execute(self)


class FakeDB:
    """In-memory trip_states + trip_state_events with fault injection.

    `fail_once[(table, op)] = "lost_response"` commits the write, then raises
    httpx.ReadError — the exact ambiguity of a dropped response.
    """

    def __init__(self, state_row):
        self.states = {state_row["id"]: state_row}
        self.events: list[dict] = []
        self.fail_once: dict[tuple[str, str], str] = {}
        self.fail_always: set[tuple[str, str]] = set()

    def table(self, name):
        return _Query(self, name)

    def execute(self, q: _Query):
        if (q.name, q.op) in self.fail_always:
            raise httpx.ReadError("[SSL: SSLV3_ALERT_BAD_RECORD_MAC] simulated")
        mode = self.fail_once.pop((q.name, q.op), None)
        result = self._run(q)
        if mode == "lost_response":
            raise httpx.ReadError("[SSL: SSLV3_ALERT_BAD_RECORD_MAC] simulated")
        return result

    def _run(self, q: _Query):
        if q.name == "trip_states":
            if q.op == "select":
                row = self.states.get(q.eqs.get("id"))
                return _Res([copy.deepcopy(row)] if row else [])
            if q.op == "update":
                row = self.states.get(q.eqs.get("id"))
                if row is None or ("last_seq" in q.eqs and row.get("last_seq") != q.eqs["last_seq"]):
                    return _Res([])          # CAS miss
                row.update(copy.deepcopy(q.payload))
                return _Res([copy.deepcopy(row)])
        if q.name == "trip_state_events":
            if q.op == "insert":
                key = (q.payload["trip_id"], q.payload["seq"])
                if any((e["trip_id"], e["seq"]) == key for e in self.events):
                    raise APIError({"message": "duplicate key value violates unique constraint",
                                    "code": "23505", "hint": None, "details": str(key)})
                self.events.append(copy.deepcopy(q.payload))
                return _Res([copy.deepcopy(q.payload)])
            if q.op == "select":
                rows = [e for e in self.events if e["trip_id"] == q.eqs.get("trip_id")]
                if "seq" in q.eqs:
                    rows = [e for e in rows if e["seq"] == q.eqs["seq"]]
                if "seq" in q.gts:
                    rows = [e for e in rows if e["seq"] > q.gts["seq"]]
                return _Res(copy.deepcopy(sorted(rows, key=lambda e: e["seq"])))
        raise AssertionError(f"unhandled fake query {q.name}.{q.op}")


TRIP = "trip-1"


@pytest.fixture()
def db(monkeypatch):
    fake = FakeDB({"id": TRIP, "state": {}, "last_seq": 0, "status": "generating", "title": "t"})
    monkeypatch.setattr(store, "get_db", lambda: fake)
    monkeypatch.setattr(store, "reset_db", lambda: None)
    monkeypatch.setattr(store.time, "sleep", lambda s: None)
    return fake


def _patch(value="hello"):
    return [PatchOp(op="set", path="/meta/title", value=value)]


# ── Tests ────────────────────────────────────────────────────────────────────

def test_normal_append(db):
    state = store.append_events(TRIP, "supervisor", _patch(), phase="p1")
    assert state["meta"]["title"] == "hello"
    assert [e["seq"] for e in db.events] == [1]
    assert db.states[TRIP]["last_seq"] == 1
    assert db.states[TRIP]["state"]["meta"]["title"] == "hello"


def test_lost_insert_response_does_not_duplicate_event(db):
    # INSERT commits but the response is dropped → retry must recognise its own
    # committed event (not re-insert, not lose it from the snapshot).
    db.fail_once[("trip_state_events", "insert")] = "lost_response"
    state = store.append_events(TRIP, "supervisor", _patch(), phase="p1")
    assert [e["seq"] for e in db.events] == [1]           # exactly one event
    assert db.states[TRIP]["last_seq"] == 1
    assert db.states[TRIP]["state"]["meta"]["title"] == "hello"
    assert state["meta"]["title"] == "hello"


def test_lost_update_response_is_a_noop_on_retry(db):
    # Both writes committed, only the UPDATE's response dropped → retry sees a
    # fully-absorbed identical tail event and does nothing.
    db.fail_once[("trip_states", "update")] = "lost_response"
    state = store.append_events(TRIP, "supervisor", _patch(), phase="p1")
    assert [e["seq"] for e in db.events] == [1]
    assert db.states[TRIP]["last_seq"] == 1
    assert state["meta"]["title"] == "hello"


def test_orphaned_foreign_event_is_replayed_not_clobbered(db):
    # Another writer's INSERT committed but its snapshot update never ran
    # (the production log's journey → itinerary sequence). The next writer
    # must not reuse seq=1 and must fold the orphan's patches into the snapshot.
    db.events.append({
        "trip_id": TRIP, "seq": 1, "actor": "supervisor", "phase": "journey",
        "patches": [{"op": "set", "path": "/transport/mode", "value": "bus"}],
        "reasoning": None,
    })
    store.append_events(TRIP, "supervisor", _patch("Rishikesh"), phase="itinerary")
    assert [e["seq"] for e in db.events] == [1, 2]        # no duplicate-key crash
    snap = db.states[TRIP]["state"]
    assert snap["transport"]["mode"] == "bus"             # orphan replayed
    assert snap["meta"]["title"] == "Rishikesh"
    assert db.states[TRIP]["last_seq"] == 2


def test_transport_error_exhausts_attempts_and_raises(db):
    db.fail_always.add(("trip_states", "select"))         # every attempt's read fails
    with pytest.raises(httpx.ReadError):
        store.append_events(TRIP, "supervisor", _patch(), phase="p1")
    assert db.events == []                                 # nothing half-written
