-- Marketing that can be reused.
--
-- `marketing_campaigns` already exists and is a good table — for *outbound
-- messaging*: writing to guests the hotel already has, on WhatsApp, with consent
-- and frequency capping. It is not an advertising system and should not be bent
-- into one. An ad has no recipient list, no consent question and no 24-hour
-- window; it has a budget, a creative, an audience definition and a state that
-- belongs to Meta rather than to us.
--
-- So: four new tables, and `marketing_campaigns` is left alone.
--
-- **The status vocabulary is the honest half of this migration.** §28 and §29
-- are emphatic: never tell a hotel an ad is published unless Meta says it is.
-- `draft` is ours. `connected` means an ad account is linked and this campaign
-- could be pushed. `published`, `running`, `paused` and `failed` are only ever
-- written from a Meta API response, and `external_id` is null in every state
-- before `published`. A campaign with no `external_id` has not been created
-- anywhere but here, and the screen says so.

create table if not exists public.marketing_profiles (
  property_id   uuid primary key references public.properties(id) on delete cascade,
  partner_id    uuid not null references public.partners(id) on delete cascade,

  usp           text,
  best_for      text[] not null default '{}',
  ideal_guest   text,
  popular_with  text[] not null default '{}',
  nearby        jsonb not null default '[]'::jsonb,

  -- Which months are strong, which days are strong. Feeds the ad copy generator
  -- and the revenue recommendations that already exist.
  best_months   smallint[] not null default '{}',
  weekend_demand text check (weekend_demand is null or weekend_demand in ('low','medium','high')),
  weekday_demand text check (weekday_demand is null or weekday_demand in ('low','medium','high')),
  seasonality   text,

  tone          text,
  languages     text[] not null default '{}',

  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

create table if not exists public.ad_audiences (
  id            uuid primary key default gen_random_uuid(),
  property_id   uuid not null references public.properties(id) on delete cascade,
  partner_id    uuid not null references public.partners(id) on delete cascade,

  name          text not null,
  age_min       smallint check (age_min is null or age_min between 13 and 65),
  age_max       smallint check (age_max is null or age_max between 13 and 65),
  genders       text[] not null default '{}',
  locations     jsonb not null default '[]'::jsonb,
  interests     text[] not null default '{}',
  travel_intent text[] not null default '{}',

  -- What Meta actually accepted for this audience, once it has been used. Null
  -- until then. Kept because Meta's interest taxonomy is not ours and the
  -- mapping is the thing that breaks when they change it.
  meta_spec     jsonb,

  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),

  unique (property_id, name),
  constraint ad_audiences_age_range check (age_min is null or age_max is null or age_max >= age_min)
);

create table if not exists public.ad_campaigns (
  id            uuid primary key default gen_random_uuid(),
  property_id   uuid not null references public.properties(id) on delete cascade,
  partner_id    uuid not null references public.partners(id) on delete cascade,

  name          text not null,
  -- Where the ad runs and what it asks the viewer to do.
  channel       text not null check (channel in ('instagram','facebook','whatsapp')),
  objective     text not null check (objective in (
                  'messages','traffic','awareness','engagement','leads')),
  destination   text not null default 'whatsapp'
                check (destination in ('whatsapp','instagram_dm','website','hotel_page')),

  audience_id   uuid references public.ad_audiences(id) on delete set null,

  -- What is being advertised. All optional and at most one is expected; the
  -- copy generator reads whichever is set and refuses to invent claims about
  -- anything that is not.
  room_type_id  uuid references public.room_types(id) on delete set null,
  experience_id uuid references public.experiences(id) on delete set null,
  offer         jsonb not null default '{}'::jsonb,

  starts_on     date,
  ends_on       date,
  daily_budget_paise integer check (daily_budget_paise is null or daily_budget_paise >= 0),

  status        text not null default 'draft' check (status in (
                  'draft','connected','published','running','paused','failed')),
  -- Meta's id for this campaign. Null in every state before 'published', and
  -- that invariant is what makes the status column trustworthy.
  external_id   text,
  external_ref  jsonb not null default '{}'::jsonb,
  last_error    text,
  last_synced_at timestamptz,

  created_by_member uuid,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),

  constraint ad_campaigns_dates check (ends_on is null or starts_on is null or ends_on >= starts_on),
  -- Published means Meta answered. Nothing else may claim it.
  constraint ad_campaigns_published_has_id check (
    status in ('draft','connected','failed') or external_id is not null
  )
);

create index if not exists ad_campaigns_property_idx
  on public.ad_campaigns (property_id, status, created_at desc);

create table if not exists public.ad_creatives (
  id            uuid primary key default gen_random_uuid(),
  campaign_id   uuid not null references public.ad_campaigns(id) on delete cascade,
  property_id   uuid not null references public.properties(id) on delete cascade,
  partner_id    uuid not null references public.partners(id) on delete cascade,

  format        text not null check (format in ('single_image','carousel','video','existing_post')),

  headline      text,
  primary_text  text,
  cta           text,

  -- For single_image and video: one entry. For carousel: up to ten, in order,
  -- each with its own caption. For existing_post: the IG media id the hotel
  -- already published, and nothing else.
  media         jsonb not null default '[]'::jsonb,
  instagram_media_id text,

  -- Which template produced the first draft, so a hotelier can regenerate
  -- without losing what they edited: `edited` records whether they have.
  template_key  text,
  edited        boolean not null default false,

  is_primary    boolean not null default true,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

create index if not exists ad_creatives_campaign_idx on public.ad_creatives (campaign_id);

alter table public.marketing_profiles enable row level security;
alter table public.ad_audiences enable row level security;
alter table public.ad_campaigns enable row level security;
alter table public.ad_creatives enable row level security;

do $$ begin
  create policy marketing_profiles_read on public.marketing_profiles
    for select using (owns_partner(partner_id));
exception when duplicate_object then null; end $$;
do $$ begin
  create policy marketing_profiles_write on public.marketing_profiles
    for all using (has_partner_perm(partner_id, 'marketing'))
    with check (has_partner_perm(partner_id, 'marketing'));
exception when duplicate_object then null; end $$;

do $$ begin
  create policy ad_audiences_read on public.ad_audiences
    for select using (owns_partner(partner_id));
exception when duplicate_object then null; end $$;
do $$ begin
  create policy ad_audiences_write on public.ad_audiences
    for all using (has_partner_perm(partner_id, 'marketing'))
    with check (has_partner_perm(partner_id, 'marketing'));
exception when duplicate_object then null; end $$;

do $$ begin
  create policy ad_campaigns_read on public.ad_campaigns
    for select using (owns_partner(partner_id));
exception when duplicate_object then null; end $$;
do $$ begin
  create policy ad_campaigns_write on public.ad_campaigns
    for all using (has_partner_perm(partner_id, 'marketing'))
    with check (has_partner_perm(partner_id, 'marketing'));
exception when duplicate_object then null; end $$;

do $$ begin
  create policy ad_creatives_read on public.ad_creatives
    for select using (owns_partner(partner_id));
exception when duplicate_object then null; end $$;
do $$ begin
  create policy ad_creatives_write on public.ad_creatives
    for all using (has_partner_perm(partner_id, 'marketing'))
    with check (has_partner_perm(partner_id, 'marketing'));
exception when duplicate_object then null; end $$;

comment on column public.ad_campaigns.external_id is
  'Meta''s campaign id. Null in every state before published — the check '
  'constraint enforces it — so "published" can never be a claim we made up.';
