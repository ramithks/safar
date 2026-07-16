"""
TripDoc — a trip as one plain JSON document (docs/tripstate_removal_plan.md).

No mutable TripState, no event sourcing, no patch protocol, no graph
synchronization, no snapshots-on-read, no replay. The backend generates or
edits the JSON, saves it, and returns it. Selections are a single-column
UPDATE that never invokes planning logic.

Modules:
  serialize  — in-memory TripGraph → doc, option/hotel trimming, budget math
  store      — plain get/put on trip_states.doc (idempotent writes)
  select     — pure doc mutations for instant UI actions
  generate   — the doc-writing generation runner
  mytrips    — My Trips mirror row from a doc
"""
