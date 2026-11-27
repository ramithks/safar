-- Who changed the floor price.
--
-- `admin_audit_log` records what Safar staff do to a hotel. Nothing records
-- what a hotel does to itself, and §42 names exactly the fields where that
-- matters: prices, the negotiation floor, B2B and group rates, payment rules,
-- cancellation terms, team permissions. Those are the changes that are noticed a
-- month later, by which time nobody remembers making them.
--
-- Append-only, one row per changed field rather than per request. A hotelier
-- asking "when did our floor drop to ₹3,800" wants the field, not a diff of a
-- form submission they have to read.

create table if not exists public.hotel_config_audit (
  id           bigserial primary key,
  property_id  uuid not null references public.properties(id) on delete cascade,
  partner_id   uuid not null references public.partners(id) on delete cascade,

  -- Which table and row. Text rather than a foreign key on purpose: this table
  -- outlives the rows it describes, and a rate plan somebody deleted is exactly
  -- the one you want the history of.
  entity       text not null,
  entity_id    uuid,
  entity_label text,

  field        text not null,
  old_value    text,
  new_value    text,

  -- Who. A member id when a person did it; an agent action id when an approval
  -- executed. Never both, and never neither.
  actor_member uuid,
  actor_agent_action uuid references public.agent_actions(id) on delete set null,
  actor_label  text not null default 'member',

  at           timestamptz not null default now(),

  constraint hotel_config_audit_actor check (
    actor_member is not null or actor_agent_action is not null
  )
);

create index if not exists hotel_config_audit_property_idx
  on public.hotel_config_audit (property_id, at desc);
create index if not exists hotel_config_audit_entity_idx
  on public.hotel_config_audit (entity, entity_id, at desc);

alter table public.hotel_config_audit enable row level security;

-- Readable by anyone who may change what it records; written only by the service
-- role. A client that could insert here could write a history that never
-- happened, which is worse than having no history.
do $$ begin
  drop policy if exists hotel_config_audit_read on public.hotel_config_audit;
drop policy if exists hotel_config_audit_read on public.hotel_config_audit;
create policy hotel_config_audit_read on public.hotel_config_audit
    for select using (has_partner_perm(partner_id, 'rates'));
exception when duplicate_object then null; end $$;

comment on table public.hotel_config_audit is
  'Append-only history of commercially sensitive hotel configuration changes. '
  'One row per field. No write policy: the service role is the only writer.';
