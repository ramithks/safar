-- A quote remembers which rule priced it.
--
-- `quotes.nightly_paise` records what each night cost, which makes every price
-- shown reconstructible — but not explicable. A hotelier looking at a group
-- booking three weeks later can see the rate was ₹4,500 and cannot see that it
-- was ₹4,500 because their own 6–9 room band takes 10% off ₹5,000. The
-- difference between those two is whether they trust the system.
--
-- Nullable, and null on every quote that predates volume pricing and on every
-- ordinary one-room quote afterwards. ON DELETE SET NULL rather than cascade:
-- deleting a tier must never delete the quotes it priced.

alter table public.quotes
  add column if not exists group_tier_id uuid
    references public.group_rate_tiers(id) on delete set null;

comment on column public.quotes.group_tier_id is
  'Which volume band produced this price, when one did. Null on an ordinary '
  'quote. Explanatory only — the money itself is in nightly_paise and the '
  'totals, which are never re-derived from this.';
