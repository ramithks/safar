"""
TravBuddy recommendation engine.

A thin, fast, reliable layer over the data the scrapers already produce. Design +
rationale live in ``docs/recommendation_engine.md``. Guiding rules, enforced across
every module here:

  * FAST     — deterministic scoring ships the ranked list the instant scrapers
               return; the LLM only ever sees a shortlist (≤ 8) and is streamed.
  * RELIABLE — nothing in this package raises into the hot path. Every external
               call (LLM, Redis, pgvector, DB) is wrapped and DEGRADES: a missing
               signal is renormalised out (see app.services.scoring._weighted_present),
               never a crash and never a zero that buries a candidate.
  * CHEAP    — personality tags come from embedding→cluster (LLM only on low
               confidence), embeddings/profiles regenerate only on doc-hash change,
               intent + profile + explanations are cached.

The public entry points are the ``/recommend/*`` endpoints (app.api.v1.recommend);
this package holds the logic they compose.
"""
