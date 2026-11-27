-- Fresh-project fix: upstream serves /api/v1/hostels from public.hostels but never
-- committed the table. Minimal shape from app/api/v1/hostels/endpoint.py usage.
-- Default-deny RLS: catalog reads go through the service role, which bypasses RLS.
create table if not exists public.hostels (
  id uuid primary key default gen_random_uuid(),
  source text,
  source_hostel_id text,
  slug text unique,
  name text,
  full_name text,
  category text,
  brand text,
  city text,
  state text,
  destination text,
  lat double precision,
  lon double precision,
  card_image text,
  banner_image text,
  tags text[],
  amenities text[],
  starting_price numeric,
  pvtroom_starting_price numeric,
  currency text default 'INR',
  check_in_time text,
  check_out_time text,
  is_active boolean default true
);
alter table public.hostels enable row level security;
