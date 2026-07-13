# Trip Graph — the canonical trip domain model

> Status: design + Phase 0 in progress. This is the reference contract for the
> multi-city trip model. Read it before touching `app/trip_state/graph.py`,
> `builder.py`, or the transport projection.

## Why

The original TripState was **single-destination-anchored**: one `origin`, one
`destination`, a `going` journey and a `return_journey`, plus a `waypoint` hack
(`onward_segments`) that inserted a stop *between* origin and destination. It
cannot express a real multi-city trip like:

```
A --fly--> B  ·  2 days in B  ·  B --train--> C  ·  3 days in C  ·  C --?--> A
```

nor the case where **no direct C→A route exists** and we must route through a
nearby gateway city D (`C → D → A`).

So the trip is now modelled as a **graph**.

## The model

A trip is a directed graph. This is permanent — not a flag, not a parallel mode.
A plain one-destination trip is simply a 2-node graph.

```
TripGraph
  nodes: [TripStop]     # cities you spend time in (+ origin / terminal)
  edges: [TripLeg]      # how you get from one node to the next
  journey_summary       # DERIVED breadcrumb, e.g. "A → B → C → A"
```

### Node — `TripStop`

A place you stop. Owns its itinerary and its stay, and reserves ownership of
bookings / documents / expenses for later (so those features need no further
model migration).

```
TripStop
  id                    # stable, e.g. "stop-2"
  city; normalized_city
  role                  # origin | stop | terminal
  coordinates {lat,lng} # geocoded once, cached on the node
  arrival_date; departure_date; nights
  days: [TripDay]       # CANONICAL itinerary — Stop → Day → Activity
  stay: Stay
  bookings: []          # reserved
  documents: []         # reserved
  expenses: []          # reserved
```

`TripDay` → `[Activity]`. **Stop → Day → Activity is the canonical itinerary
structure.** The flat top-level `itinerary.days` is a *derived compatibility
projection* (see below), not the source of truth.

### Edge — `TripLeg`

How you move between two nodes. Holds the **underlying search results** and the
**user's selection** — nothing else. It never stores a "recommended" flag.

```
TripLeg
  id
  from_node_id; to_node_id; date
  routes: [RouteCandidate]   # candidate ways to make this hop (direct + gateway)
  selection: {route_id, per_hop_option_ids: [str]} | None   # ← the ONLY stored choice
  status: routed | no_route
  bookings: []               # reserved
  expenses: []               # reserved

RouteCandidate
  id; kind: direct | via_gateway; gateway_city?
  hops: [RouteHop]           # 1 hop for direct, 2 for a gateway route (C→D, D→A)

RouteHop
  from_city; to_city; date
  options: [TransportOption] # every mode, every live result — the raw search data

TransportOption
  id; mode: flight|train|bus
  operator; departure; arrival; duration_min
  fare_per_person; currency
  raw: {}                    # provider-shaped original, kept verbatim
```

## Core principles

### 1. Recommendation is derived — never persisted

The graph stores only `RouteHop.options` (search results) and `TripLeg.selection`
(the user's explicit pick). There is **no** `recommended` / `is_recommended`
field in the canonical model, and none is ever written to the DB.

The suggestion is computed every time it is needed by a pure ranking engine:

```
rank_routes(leg, prefer, budget) -> ordered [RouteCandidate]
rank_options(hop, prefer, budget) -> ordered [TransportOption]
```

If `selection` is set, that's what's booked/priced. If not, the top of the
ranking is shown as a suggestion. Same for stays: `Stay` holds
`search_results` + `selected_id`; the recommended stay is derived.

### 2. Projections are compatibility layers, not truth

Everything the current frontend reads is regenerated from the graph on demand
by `project(state)` and is explicitly a **compat layer** to be removed once the
app reads the graph directly:

| Legacy field (compat)              | Derived from                                  |
| ---------------------------------- | --------------------------------------------- |
| `transport.going` / `return_journey` | first outbound edge / last inbound edge     |
| `transport.onward_segments`        | intermediate outbound edges                   |
| `transport.outbound_chain`         | node city sequence                            |
| `itinerary.days` (flat)            | all nodes' `days`, renumbered                 |
| `costs`                            | selected/derived option per hop + stays       |

`project()` is the *single* place recommendation is materialized, and it runs at
read time (the trip-state GET) and at SSE-emit time. The persisted snapshot's
source of truth is the graph; projected fields are a convenience mirror for the
current client and carry no authority.

### 3. Lazy migration — no backfill

On load, if a trip row has no `graph` but has legacy `transport`/`itinerary`,
`derive_graph_from_legacy(state)` builds the canonical graph once. Existing trips
upgrade the first time they're opened. There is no migration script.

### 4. Gateway routing is parallel, not a fallback

When adding a leg, direct routing and gateway discovery run **concurrently**.
Both land as `RouteCandidate`s on the edge, so the ranking engine can proactively
recommend a cheaper/faster multi-leg route even when a direct one exists ("direct
flight ₹X, or via D ₹Y and 40 min slower"). The classic no-route case is just the
degenerate form: gateways are the only candidates, so the copilot offers 2-3.

## Build phases

- **Phase 0** — `graph.py`: models, `derive_graph_from_legacy`, `project()`,
  `rank_routes`/`rank_options`, cost-from-graph; wire lazy migration + read-time
  projection. No behavior change — projections reproduce current output.
- **Phase 1** — `builder.py` + intents `add_leg`, `add_stop_itinerary`.
- **Phase 2** — `geo_nearby.py`: geocode-cached gateway finder.
- **Phase 3** — tests + A→B→C→A e2e.
