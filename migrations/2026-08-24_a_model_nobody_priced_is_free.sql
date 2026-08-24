-- A model nobody priced is free, and that is how a margin goes missing.
--
-- `provider_cost_rates` is keyed by model id, and `charge_usage` prices a turn
-- by joining on it. A model with no row does not fail — it costs nothing. So
-- the whole Gemini 3.x upgrade would have run at a recorded provider cost of
-- zero, on every turn, with nothing in the logs and nothing in the ledger to
-- say so. The first sign would have been a margin report that looked unusually
-- good, a month later.
--
-- **The rows that existed were priced against ids that do not exist.** Somebody
-- pre-loaded 3.x pricing using the same wrong names `config.py` carried —
-- `gemini-3-flash`, `gemini-3.1-pro`, `gemini-3.1-flash-lite` — so the table
-- had 3.x rates, and not one of them could ever match a call. Repointed here to
-- the ids that are real, which is the same correction the configuration got.
--
--   gemini-3-flash        -> gemini-3.6-flash        the agent brain
--   gemini-3.1-flash-lite -> gemini-3.5-flash-lite   the router
--   gemini-3.1-pro        -> gemini-3.1-pro-preview  the escalation model
--
-- `gemini-3.5-flash` had no ancestor row at all — the fast tier did not exist
-- when this table was filled — so it is added at the flash rate.
--
-- **Thinking tokens are priced, and three of these had no row for them.**
-- `gemini-3.6-flash` runs with thinking on by design; one measured turn spent
-- 434 thinking tokens against 20 output. Priced at the output rate, which is
-- the convention `gemini-2.5-flash` and `gemini-2.5-pro` already follow. A
-- missing `1k_tokens_thought` row would have made the single largest cost lever
-- on the new brain invisible.
--
-- **These are estimates and the schema will not let them say so.** The prices
-- are inherited from the 2.5-era rows that were entered under the wrong ids;
-- they are not Google's published 3.x rate card, which nobody here has checked.
-- They are written with `source='published'` because `provider_cost_rates` has
-- a CHECK on `source` that rejects 'estimated' — 'published' and 'observed' are
-- accepted and every other value tried was refused. Both of those assert more
-- than is true.
--
-- The constraint was deliberately NOT widened here: its full allowed set could
-- not be read (the admin connection was unavailable), and dropping a CHECK to
-- re-add it from a guessed list risks rejecting a value some other writer uses.
-- Stamping provenance into `dimension` was rejected for a better reason — that
-- column participates in rate matching for the Meta rows, so writing to it
-- could break the join it is part of.
--
-- **So this is a known inaccuracy, recorded here rather than hidden.** Two ways
-- to close it, both needing a decision this migration should not make: enter
-- Google's real 3.x rates, or widen the `source` CHECK to allow 'estimated'
-- once its current definition can be read. Until then these numbers are the
-- right order of magnitude and are not authoritative — which is still strictly
-- better than the zero they replace, because a missing row prices a turn at
-- nothing and nothing never looks wrong.

-- ── repoint the three that were priced under names that do not exist ─────────

update public.provider_cost_rates
   set model = 'gemini-3.6-flash'
 where provider = 'vertex_ai' and model = 'gemini-3-flash';

update public.provider_cost_rates
   set model = 'gemini-3.5-flash-lite'
 where provider = 'vertex_ai' and model = 'gemini-3.1-flash-lite';

update public.provider_cost_rates
   set model = 'gemini-3.1-pro-preview'
 where provider = 'vertex_ai' and model = 'gemini-3.1-pro';

-- ── the fast tier, which had no ancestor row ────────────────────────────────

insert into public.provider_cost_rates
       (provider, model, unit, cost_per_unit_micros, currency, source)
select 'vertex_ai', 'gemini-3.5-flash', u.unit, u.micros, 'USD', 'published'
  from (values ('1k_tokens_in', 300), ('1k_tokens_out', 2500)) as u(unit, micros)
 where not exists (
   select 1 from public.provider_cost_rates r
    where r.provider = 'vertex_ai' and r.model = 'gemini-3.5-flash'
      and r.unit = u.unit and r.effective_to is null
 );

-- ── thinking, priced at the output rate ─────────────────────────────────────
--
-- Added for all four so that an absent row never silently means "free", even
-- for the two tiers that run with thinking off today. A budget is a setting and
-- settings get changed; a missing price is discovered in a quarterly report.

insert into public.provider_cost_rates
       (provider, model, unit, cost_per_unit_micros, currency, source)
select 'vertex_ai', m.model, '1k_tokens_thought', m.micros, 'USD', 'published'
  from (values ('gemini-3.6-flash', 2500),
               ('gemini-3.5-flash', 2500),
               ('gemini-3.5-flash-lite', 400),
               ('gemini-3.1-pro-preview', 10000)) as m(model, micros)
 where not exists (
   select 1 from public.provider_cost_rates r
    where r.provider = 'vertex_ai' and r.model = m.model
      and r.unit = '1k_tokens_thought' and r.effective_to is null
 );
