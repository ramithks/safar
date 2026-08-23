-- The platform fee is a number, not a conversation.
--
-- StayOS charges 4% of the booking amount, and that fact appears nowhere in the
-- codebase. Grepped before writing this: no `platform_fee`, no `payout`, no
-- `settlement`. It exists in a pitch deck and in Sahil's head, which means every
-- number a hotelier has ever been shown about what they will receive was either
-- absent or arrived at by somebody with a calculator.
--
-- **Basis points, not a percentage.** 400 bps is 4%. A percentage column forces
-- the day somebody wants 3.5% into either a float — which has no business
-- anywhere near money — or a schema change. Integer bps costs nothing now and
-- removes that day entirely.
--
-- **Per partner, defaulted.** The rate is on `partners` because a deal is struck
-- with a hotel group, not with one of its properties, and because that is where
-- `subscription_status` and the rest of the commercial relationship already
-- lives.
--
-- **Snapshotted on the reservation, and never recomputed.** §37: a booking must
-- preserve its own financial truth. If TravBuddy renegotiates a hotel to 3%
-- next year, last year's settled bookings must keep saying 4%, because that is
-- what was settled. The columns are written once at creation and are not
-- derived on read anywhere.

alter table public.partners
  add column if not exists platform_fee_bps integer not null default 400;

do $$ begin
  alter table public.partners add constraint partners_platform_fee_check
    check (platform_fee_bps between 0 and 10000);
exception when duplicate_object then null; end $$;

alter table public.reservations
  add column if not exists platform_fee_bps integer,
  add column if not exists platform_fee_paise integer,
  add column if not exists payout_paise integer;

do $$ begin
  alter table public.reservations add constraint reservations_fee_check
    check (
      (platform_fee_paise is null or platform_fee_paise >= 0)
      and (payout_paise is null or payout_paise >= 0)
      and (platform_fee_bps is null or platform_fee_bps between 0 and 10000)
    );
exception when duplicate_object then null; end $$;

-- Backfill the 72 rows that predate this.
--
-- Honest about what it is: a *reconstruction* at today's default rate from each
-- booking's own stored total, not a record of what was actually charged, because
-- nothing was actually charged. It is written once, here, so that settlement
-- reporting has a complete column rather than one with a hole in its history —
-- and after this migration nothing recomputes it again, so a later rate change
-- cannot rewrite these numbers a second time.
-- The casts are load-bearing, and the first attempt at this migration failed
-- without them: `total_paise * 400` is integer × integer, which overflows a
-- 32-bit integer at a booking total of about ₹53 lakh — *before* the division
-- that would have brought it back into range. The arithmetic is done in numeric
-- and rounded back to an integer at the end.
update public.reservations r
set platform_fee_bps   = coalesce(p.platform_fee_bps, 400),
    platform_fee_paise = round(r.total_paise::numeric * coalesce(p.platform_fee_bps, 400)::numeric / 10000)::integer,
    payout_paise       = r.total_paise
                         - round(r.total_paise::numeric * coalesce(p.platform_fee_bps, 400)::numeric / 10000)::integer
from public.partners p
where p.id = r.partner_id and r.platform_fee_paise is null;

comment on column public.reservations.platform_fee_paise is
  'The StayOS fee on this booking, snapshotted at creation from the partner''s '
  'rate at that moment. Never recomputed — a later rate change must not rewrite '
  'a settled booking. Internal: never shown to a guest.';
comment on column public.partners.platform_fee_bps is
  'Basis points. 400 = 4%. The current rate; what a given booking was charged '
  'lives on the reservation.';
