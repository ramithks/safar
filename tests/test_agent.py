"""
Offline tests for the copilot agent loop (app.trip_state.agent).

The planner LLM is scripted (a queue of decisions) and every live pipeline is
stubbed the same way tests/test_builder.py does, so what's exercised is the
LOOP: plan → execute a chain of tools → observe → re-plan → finish/ask. The
headline test is the exact failure that motivated the agent: a compound
multi-city message that the old single-intent router collapsed to one leg.
"""
import asyncio
import json

import pytest

from app.trip_state import agent, builder, graph as G


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _offline_geocode(monkeypatch):
    """Stop map geocoding hitting Places for made-up test cities."""
    async def _none(city):
        return None

    monkeypatch.setattr("app.services.geo_nearby.geocode_city", _none)


def _train_block(fare=800, dur=180, name="Shatabdi"):
    return {"trains": {"items": [
        {"train_name": name, "cheapest_fare": fare, "duration_min": dur,
         "departure_time": "06:00", "arrival_time": "09:00"},
    ]}}


def _base_state(origin="Mumbai", destination="Goa"):
    return {
        "request": {
            "origin": origin, "destination": destination,
            "departure_date": "2026-08-01", "travelers": 2,
            "budget_tier": "mid", "stay_nights": 0,
        },
        "transport": {}, "itinerary": {}, "stay": {}, "costs": {},
    }


class ScriptedPlanner:
    """generate_json stand-in: returns queued decisions, records every prompt."""

    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.prompts: list[str] = []

    async def __call__(self, prompt, **kwargs):
        self.prompts.append(prompt)
        if not self.decisions:
            raise AssertionError("planner called more times than scripted")
        return self.decisions.pop(0)


def _mock_live_pipelines(monkeypatch, journey=None, gateways=None, trip=None):
    async def fake_journey(f, t, *a, **k):
        return journey(f, t) if callable(journey) else journey

    async def fake_gateways(*a, **k):
        return gateways or []

    async def fake_trip(req, *a, **k):
        if trip:
            return trip(req)
        return {"days": [
            {"day": i + 1, "activities": [{"title": f"Spot {i + 1}"}]}
            for i in range(req.days)
        ]}

    monkeypatch.setattr(builder, "_shaped_journey", fake_journey)
    monkeypatch.setattr("app.services.geo_nearby.find_gateways", fake_gateways)
    monkeypatch.setattr("app.api.v1.plan_trip.orchestrator.run_plan_trip", fake_trip)


# ── The headline: one compound message → the whole multi-city build ────────────

def test_compound_message_builds_whole_trip(monkeypatch):
    """'Goa from Mumbai, 2 days there, then Indore for 2, Jaipur for 3, then
    back to our city' — the agent must run ALL of it, not the first clause."""
    state = _base_state()
    _mock_live_pipelines(monkeypatch, journey=_train_block())

    planner = ScriptedPlanner([
        {   # Turn 1: the full plan, legs interleaved with itineraries.
            "thought": "Chain every hop and every stay.",
            "actions": [
                {"tool": "add_leg", "leg_from": "Mumbai", "leg_to": "Goa"},
                {"tool": "add_stop_itinerary", "stop_city": "Goa", "stop_days": 2},
                {"tool": "add_leg", "leg_from": "Goa", "leg_to": "Indore"},
                {"tool": "add_stop_itinerary", "stop_city": "Indore", "stop_days": 2},
                {"tool": "add_leg", "leg_from": "Indore", "leg_to": "Jaipur"},
                {"tool": "add_stop_itinerary", "stop_city": "Jaipur", "stop_days": 3},
                {"tool": "add_leg", "leg_from": "Jaipur", "leg_to": "Mumbai"},
            ],
        },
        {   # Turn 2: everything done → grounded summary, no actions.
            "thought": "All built.",
            "actions": [],
            "reply": "Your Mumbai → Goa → Indore → Jaipur → Mumbai loop is built.",
        },
    ])
    monkeypatch.setattr(agent, "generate_json", planner)

    step_labels: list[str] = []
    result = _run(agent.run_agent(
        "i want goa as well from mumbai and 2 day want to stay in goa and then "
        "from goa to indore and then stay in indore 2 days and then indore to "
        "jaipur for 3 days in jaipur and then return back to our city",
        state, "u", on_step=lambda s: step_labels.append(s["label"]),
    ))

    # Every action ran and surfaced live progress.
    assert len(step_labels) == 7
    assert result["applied"] is True
    assert result["intent"] == "multi_edit"
    assert len(result["steps"]) == 7
    assert "Mumbai → Goa → Indore → Jaipur → Mumbai" in result["reply"]

    # The graph really holds the full loop: 4 cities, 4 legs, return to origin.
    g = G.TripGraph.model_validate(state["graph"])
    assert [n.city for n in g.nodes] == ["Mumbai", "Goa", "Indore", "Jaipur"]
    assert len(g.edges) == 4
    assert g.edges[-1].to_node_id == g.nodes[0].id
    # 2 + 2 + 3 itinerary days, renumbered end to end.
    assert [d["day"] for d in state["itinerary"]["days"]] == [1, 2, 3, 4, 5, 6, 7]
    # Turn 2 saw the refreshed graph + observations from turn 1.
    assert "TRIP GRAPH built so far" in planner.prompts[1]
    assert "THIS RUN SO FAR" in planner.prompts[1]
    assert "is_recommended" not in json.dumps(state["graph"])


# ── Pure Q&A: no actions, one LLM call, nothing mutated ────────────────────────

def test_question_answers_without_tools(monkeypatch):
    state = _base_state()
    planner = ScriptedPlanner([
        {"thought": "Just a question.", "actions": [],
         "reply": "The train leaves at 06:00 and costs ₹800/person."},
    ])
    monkeypatch.setattr(agent, "generate_json", planner)

    result = _run(agent.run_agent("what time is the train?", state, "u"))

    assert len(planner.prompts) == 1
    assert result["applied"] is False
    assert result["intent"] == "answer_question"
    assert result["patches"] == []
    assert "06:00" in result["reply"]


# ── Gateway offer: tool yields choices → agent re-plans → asks the user ────────

def test_no_route_gateway_offer_reaches_user(monkeypatch):
    state = _base_state()
    _mock_live_pipelines(
        monkeypatch, journey=None,   # no direct route anywhere
        gateways=[{"city": "Pune", "distance_km": 120, "mode": "train",
                   "fare_per_person": 500, "search": {}}],
    )
    planner = ScriptedPlanner([
        {"thought": "Add the leg.", "actions": [
            {"tool": "add_leg", "leg_from": "Alibaug", "leg_to": "Jaipur"},
        ]},
        {   # The observation lists gateways; the model puts them to the user.
            "thought": "No direct route — ask which gateway.",
            "actions": [],
            "reply": "No direct route from Alibaug — route through Pune?",
            "clarify_options": ["Via Pune"],
        },
    ])
    monkeypatch.setattr(agent, "generate_json", planner)

    result = _run(agent.run_agent("get me from Alibaug to Jaipur", state, "u"))

    # The gateway offer flowed through the observation into the re-plan…
    assert "Pune" in planner.prompts[1]
    # …and out to the user as chips, with nothing half-applied.
    assert result["clarify_options"] == ["Via Pune"]
    assert result["intent"] == "clarify"
    assert result["applied"] is False


# ── A failed tool becomes an observation, not a crash ──────────────────────────

def test_tool_failure_is_observed_and_reported(monkeypatch):
    state = _base_state()

    async def boom(*a, **k):
        raise RuntimeError("scraper down")

    monkeypatch.setattr(builder, "handle_add_stop_itinerary", boom)
    planner = ScriptedPlanner([
        {"thought": "Build days.", "actions": [
            {"tool": "add_stop_itinerary", "stop_city": "Goa", "stop_days": 2},
        ]},
        {"thought": "It failed — tell the user.", "actions": [],
         "reply": "Goa's itinerary service hiccuped — try again in a moment."},
    ])
    monkeypatch.setattr(agent, "generate_json", planner)

    result = _run(agent.run_agent("2 days in goa", state, "u"))

    assert "failed" in planner.prompts[1]          # observation reached the re-plan
    assert result["applied"] is False
    assert "hiccuped" in result["reply"]


# ── Graph grounding: the agent can pick a ride on any leg it built ─────────────

def test_agent_selects_option_on_graph_leg(monkeypatch):
    state = _base_state()

    async def two_trains(f, t, *a, **k):
        return {"trains": {"items": [
            {"train_name": "Shatabdi", "cheapest_fare": 800, "duration_min": 180},
            {"train_name": "Duronto", "cheapest_fare": 1200, "duration_min": 150},
        ]}}

    async def no_gateways(*a, **k):
        return []

    monkeypatch.setattr(builder, "_shaped_journey", two_trains)
    monkeypatch.setattr("app.services.geo_nearby.find_gateways", no_gateways)

    planner = ScriptedPlanner([
        {"thought": "Add the leg, then honour the faster-train ask.", "actions": [
            {"tool": "add_leg", "leg_from": "Mumbai", "leg_to": "Goa"},
            {"tool": "select_leg_option", "leg_id": "leg-0", "mode": "train", "option_index": 1},
        ]},
        {"thought": "done", "actions": [], "reply": "Booked the faster Duronto."},
    ])
    monkeypatch.setattr(agent, "generate_json", planner)

    result = _run(agent.run_agent("goa by the faster train", state, "u"))

    assert result["applied"] is True
    g = G.TripGraph.model_validate(state["graph"])
    assert g.edges[0].selection.per_hop_option_ids[0] == "train-1"
    # The grounding block lists legs with ids + numbered options for the re-plan.
    assert "leg leg-0" in planner.prompts[1]
    assert "←picked" in planner.prompts[1]


# ── Budget: a runaway plan stops at MAX_ACTIONS and reports honestly ───────────

def test_action_budget_caps_runaway_plans(monkeypatch):
    state = _base_state()
    _mock_live_pipelines(monkeypatch, journey=_train_block())

    hops = [{"tool": "add_leg", "leg_from": f"C{i}", "leg_to": f"C{i + 1}"}
            for i in range(agent.MAX_ACTIONS + 5)]
    planner = ScriptedPlanner(
        [{"thought": "huge", "actions": hops}]
        + [{"thought": "done", "actions": [], "reply": "Built what fit."}] * (agent.MAX_TURNS - 1)
    )
    monkeypatch.setattr(agent, "generate_json", planner)

    result = _run(agent.run_agent("build everything", state, "u"))

    assert len(result["steps"]) == agent.MAX_ACTIONS
    assert result["applied"] is True


# ── Reliability: a re-emitted identical call is not re-run (the search loop) ────
#
# "i want a swimming pool" made the planner re-propose the SAME search_hotels
# every turn, so a live scrape ran ~13× before giving up with "Here's how far I
# got". The loop guard runs the search once and settles on its result.

def test_repeated_search_hotels_runs_once_and_settles(monkeypatch):
    state = _base_state()

    calls = {"n": 0}

    async def fake_search(_state, _query, raw_filters=None):
        from app.trip_state.patch import PatchOp
        calls["n"] += 1
        return (
            [PatchOp(op="set", path="/stay/hotels",
                     value=[{"name": "Pool Resort", "price_per_night": 3000}])],
            "Found 12 stays near Goa — tell me which one to lock in.",
        )

    monkeypatch.setattr("app.trip_state.hotel_search.handle_search_hotels", fake_search)

    same = {"tool": "search_hotels", "hotel_query": "pool in Goa"}
    planner = ScriptedPlanner([
        {"thought": "search", "actions": [same]},
        {"thought": "search again (the loop)", "actions": [same]},
        {"thought": "and again", "actions": [same]},
        {"thought": "and again", "actions": [same]},
    ])
    monkeypatch.setattr(agent, "generate_json", planner)

    result = _run(agent.run_agent("i want a swimming pool in my hotel", state, "u"))

    assert calls["n"] == 1                       # the live search ran exactly once
    assert result["applied"] is True
    assert "Found 12 stays" in result["reply"]
    assert not result["reply"].startswith("Here's how far I got")


# ── Reliability: a re-planned turn can't stack duplicates or corrupt the trip ──
#
# The real failure: on a Bengaluru→Jaipur trip, "add Goa, 2 days, then Jaipur"
# made the model re-emit the whole plan every turn (it couldn't tell the job was
# done), so Goa was added 3-4×. Even if the planner misbehaves like that, the
# idempotent handlers must keep the graph a single clean path.

def _legacy_state():
    return {
        "request": {"origin": "Bengaluru", "destination": "Jaipur",
                    "departure_date": "2026-07-25", "return_date": "2026-07-28",
                    "travelers": 2, "budget_tier": "mid", "stay_nights": 2},
        "transport": {
            "outbound_chain": ["Bengaluru", "Jaipur"],
            "going": _train_block(fare=8000),
            "return_journey": _train_block(fare=8500),
        },
        "stay": {"hotels": [{"name": "Jaipur Palace", "price_per_night": 3000}]},
        "itinerary": {"days": [{"day": 1, "activities": [{"title": "Amber Fort"}]}]},
        "costs": {"breakdown": {}},
    }


def test_replanned_turn_does_not_duplicate_or_corrupt(monkeypatch):
    state = _legacy_state()
    _mock_live_pipelines(monkeypatch, journey=_train_block(fare=4000))

    plan = [
        {"tool": "add_leg", "leg_from": "Bengaluru", "leg_to": "Goa"},
        {"tool": "add_stop_itinerary", "stop_city": "Goa", "stop_days": 2},
        {"tool": "add_leg", "leg_from": "Goa", "leg_to": "Jaipur"},
        {"tool": "add_leg", "leg_from": "Jaipur", "leg_to": "Bengaluru"},
    ]
    planner = ScriptedPlanner([
        {"thought": "Build it.", "actions": plan},
        {"thought": "Not sure it's done — do it again.", "actions": plan},  # the loop
        {"thought": "Done.", "actions": [], "reply": "Bengaluru → Goa → Jaipur is set."},
    ])
    monkeypatch.setattr(agent, "generate_json", planner)

    result = _run(agent.run_agent("add goa, 2 days, then jaipur", state, "u"))

    assert result["applied"] is True
    g = G.TripGraph.model_validate(state["graph"])
    # Despite the planner re-emitting the whole plan, the graph stays clean:
    # travel order, no phantom direct leg, no duplicate return.
    assert [n.city for n in g.nodes] == ["Bengaluru", "Goa", "Jaipur"]
    assert {(e.from_city, e.to_city) for e in g.edges} == {
        ("Bengaluru", "Goa"), ("Goa", "Jaipur"), ("Jaipur", "Bengaluru"),
    }
    assert len(g.edges) == 3
    assert state["transport"]["journey_summary"] == "Bengaluru → Goa → Jaipur"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
