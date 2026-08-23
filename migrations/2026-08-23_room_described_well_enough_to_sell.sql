-- A room is described well enough to sell.
--
-- `room_types` carried eight useful columns: name, description, total_units,
-- max_occupancy, standard_occupancy, bed_type, size_sqft, amenities. That is
-- enough to put a room on a calendar and not enough for anyone — a guest, a
-- website, or an AI answering at 2am — to decide whether they want it.
--
-- Every column here is NULLABLE, and that is deliberate against a brief that
-- asks for the opposite. Twenty-one room types already exist in production and
-- NOT NULL would either refuse the migration or invent an answer for each of
-- them. So null means "nobody has said yet", and *readiness* — a deterministic
-- server-side check, not a database constraint — is what decides whether a room
-- may be sold. Where "no" is a real answer it is a real stored value:
-- extra_bed = 'none' is a hotel that has thought about extra beds and has none,
-- which is a different fact from a hotel that has never been asked.

alter table public.room_types
  -- Occupancy, split. max_occupancy stays as the total and is what availability
  -- already reads; these two are how "2 adults + 1 child" gets an answer.
  add column if not exists max_adults integer,
  add column if not exists max_children integer,
  add column if not exists children_allowed text,

  -- Beds. bed_type ("King bed") already exists and says nothing about how many.
  add column if not exists bed_count integer,
  add column if not exists extra_bed text,
  add column if not exists extra_bed_paise integer,
  add column if not exists crib text,
  add column if not exists crib_paise integer,

  -- Where and what it is like.
  add column if not exists floor text,
  add column if not exists view text,
  add column if not exists smoking text,
  add column if not exists accessible text,
  add column if not exists connecting text,
  add column if not exists bathroom text;

-- Closed vocabularies, checked. A three-value availability word is used for
-- everything that can be had, had on request, or not had at all — one word for
-- the agent to learn rather than three near-synonyms.
do $$ begin
  alter table public.room_types
    add constraint room_types_extra_bed_check
    check (extra_bed is null or extra_bed in ('available','on_request','none'));
exception when duplicate_object then null; end $$;

do $$ begin
  alter table public.room_types
    add constraint room_types_crib_check
    check (crib is null or crib in ('available','on_request','none'));
exception when duplicate_object then null; end $$;

do $$ begin
  alter table public.room_types
    add constraint room_types_children_check
    check (children_allowed is null or children_allowed in ('allowed','not_allowed','above_age'));
exception when duplicate_object then null; end $$;

do $$ begin
  alter table public.room_types
    add constraint room_types_smoking_check
    check (smoking is null or smoking in ('smoking','non_smoking'));
exception when duplicate_object then null; end $$;

do $$ begin
  alter table public.room_types
    add constraint room_types_yesno_check
    check (
      (accessible is null or accessible in ('yes','no'))
      and (connecting is null or connecting in ('yes','no'))
      and (bathroom is null or bathroom in ('private','shared'))
    );
exception when duplicate_object then null; end $$;

-- Money is paise and cannot be negative. A charge of null means the hotel has
-- not priced it; a charge of 0 means it is free, and those are different
-- answers a guest is owed.
do $$ begin
  alter table public.room_types
    add constraint room_types_charges_check
    check (
      (extra_bed_paise is null or extra_bed_paise >= 0)
      and (crib_paise is null or crib_paise >= 0)
      and (bed_count is null or (bed_count between 1 and 20))
      and (max_adults is null or (max_adults between 1 and 30))
      and (max_children is null or (max_children between 0 and 30))
    );
exception when duplicate_object then null; end $$;

-- Photo captions and alt text, alongside the URLs that already exist.
--
-- `photos text[]` stays exactly as it is: every reader in the codebase — the
-- website, send_room_photos, the extractor — reads it, and turning it into a
-- table would be a rewrite of all of them for a caption. The metadata sits
-- beside it, keyed by URL, and a photo with no entry simply has no caption.
alter table public.room_types
  add column if not exists photo_meta jsonb not null default '{}'::jsonb;

comment on column public.room_types.photo_meta is
  'Per-photo caption and alt text, keyed by the URL as it appears in photos[]. '
  'Absent key means no caption; the array remains the source of truth for which '
  'photos exist and in what order.';

comment on column public.room_types.max_adults is
  'Adults this room sleeps. max_occupancy remains the total and is what the '
  'availability engine reads.';
comment on column public.room_types.extra_bed is
  'available | on_request | none. "none" is an answer; null is an unanswered question.';
