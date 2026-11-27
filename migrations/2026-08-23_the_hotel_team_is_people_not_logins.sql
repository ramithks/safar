-- The hotel team is people, not logins.
--
-- `partner_members` is the login list. Its `profile_id` is NOT NULL and points
-- at a Supabase auth user, which is correct for deciding what a screen may show
-- and useless for the thing the brief actually asks for: *reach the person on
-- the front desk on WhatsApp at 11pm*. The front desk does not have an account.
-- Neither does the driver, and the owner's brother who approves discounts uses
-- a phone number that has never seen a login page.
--
-- So there are two tables and they answer two questions:
--
--     partner_members       who may sign in, and what they may do  (unchanged)
--     hotel_team_members    who to contact, how, and about what    (new)
--
-- A person who is both has a row in each, linked by `member_id`. A person who is
-- only reachable has a row here and no account, which is the common case and was
-- previously impossible to represent.

create table if not exists public.hotel_team_members (
  id           uuid primary key default gen_random_uuid(),
  property_id  uuid not null references public.properties(id) on delete cascade,
  partner_id   uuid not null references public.partners(id) on delete cascade,

  -- The same row in partner_members, when this person also signs in. Null is
  -- the normal case and not a defect.
  member_id    uuid references public.partner_members(id) on delete set null,

  name         text not null,
  role         text not null check (role in (
                 'owner','manager','front_desk','reservations',
                 'sales','accounts','marketing','operations','other')),
  department   text,

  email        text,
  -- E.164 without the plus, as every other phone column in this schema stores it.
  whatsapp     text,
  phone        text,

  is_active    boolean not null default true,
  sort_order   integer not null default 0,

  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),

  -- Somebody with no way to be reached cannot be notified, and a directory of
  -- unreachable people is how an escalation silently goes nowhere.
  constraint hotel_team_members_reachable check (
    whatsapp is not null or email is not null or phone is not null or member_id is not null
  )
);

-- One WhatsApp number per hotel. Two rows for the same number means two
-- notifications for one event, sent to one phone.
create unique index if not exists hotel_team_members_whatsapp_uniq
  on public.hotel_team_members (property_id, whatsapp) where whatsapp is not null;

create index if not exists hotel_team_members_property_idx
  on public.hotel_team_members (property_id) where is_active;

create table if not exists public.team_notification_rules (
  id             uuid primary key default gen_random_uuid(),
  property_id    uuid not null references public.properties(id) on delete cascade,
  partner_id     uuid not null references public.partners(id) on delete cascade,

  event          text not null check (event in (
                   'booking_created','payment_received','payment_failed',
                   'group_inquiry','negotiation_approval','special_request',
                   'transfer_request','cancellation','refund',
                   'high_value_booking','vip_guest','unanswered_conversation',
                   'agent_escalation')),

  team_member_id uuid not null references public.hotel_team_members(id) on delete cascade,

  channels       text[] not null default '{whatsapp}',
  is_active      boolean not null default true,

  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),

  unique (property_id, event, team_member_id),
  -- An empty channel list is a rule that fires and does nothing.
  constraint team_notification_rules_channels check (
    array_length(channels, 1) >= 1
    and channels <@ array['whatsapp','email','dashboard']::text[]
  )
);

create index if not exists team_notification_rules_lookup_idx
  on public.team_notification_rules (property_id, event) where is_active;

-- The escalation itself, and its life afterwards.
--
-- `request_human_handoff` already exists and flips `conversations.handler`, but
-- it records no *reason*, notifies nobody, and has no notion of having been
-- answered. So the same guest asking the same thing twice escalates twice, and
-- an agent that escalated an hour ago has no way to know it is still waiting.
--
-- `dedupe_key` is what stops the second one. It is computed by the caller from
-- the substance of the request — the room and the rate being argued about, not
-- the words — so a guest rephrasing does not open a second ticket.
create table if not exists public.agent_escalations (
  id              uuid primary key default gen_random_uuid(),
  property_id     uuid not null references public.properties(id) on delete cascade,
  partner_id      uuid not null references public.partners(id) on delete cascade,

  conversation_id uuid references public.conversations(id) on delete cascade,
  reservation_id  uuid references public.reservations(id) on delete set null,
  quote_id        uuid references public.quotes(id) on delete set null,
  agent_action_id uuid references public.agent_actions(id) on delete set null,
  task_id         uuid references public.tasks(id) on delete set null,

  kind            text not null check (kind in (
                    'discount_request','group_inquiry','special_request',
                    'transfer_request','payment_issue','cancellation_exception',
                    'unanswered','other')),
  dedupe_key      text not null default '',

  -- Why, in the agent's words. The *numbers* in the outgoing message never come
  -- from here — they are read server-side from the quote and the rules.
  reason          text,
  -- The structured facts the internal message was built from. Kept so a hotelier
  -- reading it a week later sees what was true at the time.
  payload         jsonb not null default '{}'::jsonb,

  team_member_id  uuid references public.hotel_team_members(id) on delete set null,
  message_sent    text,

  status          text not null default 'open'
                  check (status in ('open','notified','responded','resolved','dismissed')),
  resolution      text,

  notified_at     timestamptz,
  responded_at    timestamptz,
  resolved_at     timestamptz,
  resolved_by     uuid,

  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

-- One open escalation per conversation per subject. A closed one may be
-- reopened by a new row, which is correct: the guest came back about it.
create unique index if not exists agent_escalations_open_uniq
  on public.agent_escalations (conversation_id, kind, dedupe_key)
  where status in ('open','notified');

create index if not exists agent_escalations_property_idx
  on public.agent_escalations (property_id, status, created_at desc);

alter table public.hotel_team_members enable row level security;
alter table public.team_notification_rules enable row level security;
alter table public.agent_escalations enable row level security;

-- The directory is readable by anyone who works here — a receptionist needs the
-- manager's number. Editing it is 'members', the same permission that governs
-- the login list, because adding a WhatsApp number to the escalation path is
-- adding somebody to the hotel's staff in every sense that matters.
do $$ begin
  drop policy if exists hotel_team_members_read on public.hotel_team_members;
drop policy if exists hotel_team_members_read on public.hotel_team_members;
create policy hotel_team_members_read on public.hotel_team_members
    for select using (owns_partner(partner_id));
exception when duplicate_object then null; end $$;
do $$ begin
  drop policy if exists hotel_team_members_write on public.hotel_team_members;
drop policy if exists hotel_team_members_write on public.hotel_team_members;
create policy hotel_team_members_write on public.hotel_team_members
    for all using (has_partner_perm(partner_id, 'members'))
    with check (has_partner_perm(partner_id, 'members'));
exception when duplicate_object then null; end $$;

do $$ begin
  drop policy if exists team_notification_rules_read on public.team_notification_rules;
drop policy if exists team_notification_rules_read on public.team_notification_rules;
create policy team_notification_rules_read on public.team_notification_rules
    for select using (owns_partner(partner_id));
exception when duplicate_object then null; end $$;
do $$ begin
  drop policy if exists team_notification_rules_write on public.team_notification_rules;
drop policy if exists team_notification_rules_write on public.team_notification_rules;
create policy team_notification_rules_write on public.team_notification_rules
    for all using (has_partner_perm(partner_id, 'members'))
    with check (has_partner_perm(partner_id, 'members'));
exception when duplicate_object then null; end $$;

do $$ begin
  drop policy if exists agent_escalations_read on public.agent_escalations;
drop policy if exists agent_escalations_read on public.agent_escalations;
create policy agent_escalations_read on public.agent_escalations
    for select using (owns_partner(partner_id));
exception when duplicate_object then null; end $$;
do $$ begin
  drop policy if exists agent_escalations_write on public.agent_escalations;
drop policy if exists agent_escalations_write on public.agent_escalations;
create policy agent_escalations_write on public.agent_escalations
    for all using (has_partner_perm(partner_id, 'messaging'))
    with check (has_partner_perm(partner_id, 'messaging'));
exception when duplicate_object then null; end $$;

comment on table public.hotel_team_members is
  'Who to contact and how. Distinct from partner_members, which is who may sign '
  'in: the front desk has a WhatsApp number and no account, and until now that '
  'person could not be represented at all.';
comment on column public.agent_escalations.dedupe_key is
  'Computed from the substance of the request, never from the guest''s wording, '
  'so a rephrased question does not open a second ticket.';
