"""
Trip build machinery — what remains of the old TripState package after the
TripDoc migration (docs/tripstate_removal_plan.md).

  graph.py    — in-memory TripGraph model + ranking + legacy derivation
  builder.py  — leg search / date-cascade stabilization loop
  compose.py  — spec → graph skeleton + gateway fallback

These are GENERATION-TIME structures only: nothing here is persisted or
served. The stored representation is the plain TripDoc (app/trips_doc).
The event log, patch protocol, Redis streams, snapshots, projections and
copilot that used to live here are deleted.
"""
