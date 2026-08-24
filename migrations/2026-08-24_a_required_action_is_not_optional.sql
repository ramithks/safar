-- A required action is not optional, and a turn should say who decided.
--
-- The live replay scored between 28 and 44 of 44 assertions on identical
-- input. The two assertions that moved were always the same two — a guest
-- asking for five rooms, and a guest asking for a discount — and on the runs
-- that failed nothing was wrong with the build: the model answered instead of
-- calling the tool. `agent_actions` recorded the calls that happened and had no
-- way to record the call that should have.
--
-- So a turn now carries what was *required* of it beside what was done, and
-- every action row says whether the model chose it or the application did.
-- Without `decision_source` the two are indistinguishable after the fact, and
-- "how often does the model call negotiate_rate by itself" — the number this
-- whole pass is judged on — would not be answerable from the database.
--
-- Nullable throughout and defaulted where it means something, because every
-- row written before today was a model decision at whatever autonomy the agent
-- held, and backfilling a guess would be worse than a null that says "before
-- this was recorded".

alter table public.agent_actions
  add column if not exists decision_source text,
  add column if not exists autonomy_level smallint,
  add column if not exists intent text,
  add column if not exists human_approval_required boolean not null default false;

comment on column public.agent_actions.decision_source is
  'Who chose this call: model (the LLM asked for it), application (the '
  'deterministic required-action layer ran it because the model did not), '
  'human (a console operator or an approved proposal). Null on rows written '
  'before 2026-08-24.';

comment on column public.agent_actions.autonomy_level is
  'The agent autonomy level in force when this ran, so a decision can be read '
  'against the configuration that permitted it rather than against today''s.';

comment on column public.agent_actions.intent is
  'The deterministically detected guest intent this call answers — '
  'GROUP_BOOKING, NEGOTIATION and so on. Set on application-forced calls and '
  'on model calls that discharged a detected requirement.';

alter table public.agent_actions
  drop constraint if exists agent_actions_decision_source_check;

alter table public.agent_actions
  add constraint agent_actions_decision_source_check
  check (decision_source is null
         or decision_source = any (array['model', 'application', 'human']));

-- The run's side of the same story: what the guest's message required, and
-- whether the turn discharged it.
alter table public.agent_runs
  add column if not exists intent text,
  add column if not exists required_actions jsonb,
  add column if not exists required_action_status text;

comment on column public.agent_runs.required_actions is
  'The requirement summary for this turn: intents detected, tools required, '
  'which were satisfied, which were left unmet and which the application ran '
  'itself. Shape is services/ai/required_actions.summarise().';

alter table public.agent_runs
  drop constraint if exists agent_runs_required_action_status_check;

alter table public.agent_runs
  add constraint agent_runs_required_action_status_check
  check (required_action_status is null
         or required_action_status = any (
           array['none', 'model', 'forced', 'unmet']));

-- The two questions production asks of this, indexed because both are asked
-- over a time window across every property rather than within one run.
create index if not exists agent_runs_required_action_status_idx
  on public.agent_runs (required_action_status, started_at desc)
  where required_action_status is not null;

create index if not exists agent_actions_decision_source_idx
  on public.agent_actions (decision_source, created_at desc)
  where decision_source is not null;
