-- A lookup is not a negotiation, and the tier column has to be able to say so.
--
-- `agent_runs.model_tier` was constrained to the three tiers that existed when
-- it was written: conversation, reasoning, classify. The Gemini 3.x upgrade
-- splits the first of those in two, because it was doing two jobs — a guest
-- asking for the wifi password and a guest disputing a cancellation fee were
-- both "the workhorse", and the cheap question paid the expensive model's bill
-- on most of the day's traffic. So:
--
--   classify      the router: intent, language, spam
--   fast          FAQs, lookups, simple CRUD — the volume
--   conversation  the agent brain: tool selection, operations, money
--   deep          escalation only, after the brain has already failed
--
-- Without this the split is not merely unrecorded, it is fatal. `_open_run`
-- writes `choice.tier` into this column as the first statement of every turn,
-- before the model is called at all, so a turn routed to `fast` dies at the
-- insert:
--
--   new row for relation "agent_runs" violates check constraint
--   "agent_runs_model_tier_check"
--
-- **And the feature flag does not save it.** `HOTEL_MODELS_V3=false` reverts
-- which *model* each tier resolves to; it does not revert the routing table, so
-- `tier_of('guest_faq')` is `fast` either way. The rollback switch and this
-- migration are independent, and this one is not optional.
--
-- It went unnoticed through 2,052 passing tests because the test double has no
-- constraints. Found on a real database by a plain FAQ turn — "Breakfast kitne
-- baje tak milta hai?" — which is the cheapest possible question and the exact
-- traffic the new tier exists to serve.
--
-- Widening only. Every value previously allowed is still allowed, every row
-- already written still satisfies it, and reverting is the old ARRAY back.

alter table public.agent_runs
  drop constraint if exists agent_runs_model_tier_check;

alter table public.agent_runs
  add constraint agent_runs_model_tier_check
  check (
    model_tier is null
    or model_tier = any (array['classify', 'fast', 'conversation', 'reasoning', 'deep'])
  );

-- Nothing else enumerates a tier, and that was checked rather than assumed.
-- `usage_events` has no tier column — the value rides in the event's JSON,
-- which is unconstrained by design. `billing.USAGE_BY_AGENT_TIER` is a Python
-- dict read with a default, so an unrecognised tier prices as the agent's base
-- usage type instead of raising; the one special case, ("sales", "reasoning"),
-- is unaffected because that pair still exists.
