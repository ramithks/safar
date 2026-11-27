-- Safar fresh-project fix: upstream assumed a pre-existing public.hotels
-- "master identity" table but never committed its definition. Reconstructed
-- from app/services/hotel_store.py (writer) and app/api/v1/hotels/endpoint.py
-- (reader column lists). The 2026-07-13_reco_engine migration adds slug,
-- subscores, landmarks and canonical_hotel_id on top of this.

CREATE TABLE IF NOT EXISTS public.hotels (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source           text NOT NULL,
  source_hotel_id  text NOT NULL,
  name             text NOT NULL,
  address          text,
  city             text,
  destination      text,
  lat              double precision,
  lon              double precision,
  star_rating      numeric,
  review_score     numeric,
  review_count     integer,
  review_label     text,
  property_type    text,
  amenities        jsonb,
  photos           jsonb,
  booking_url      text,
  map_url          text,
  description      text,
  subscores        jsonb,
  landmarks        jsonb,
  quality_score    numeric,
  price_hint       numeric,
  last_seen_at     timestamptz,
  updated_at       timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source, source_hotel_id)
);

CREATE INDEX IF NOT EXISTS hotels_city_idx ON public.hotels (city);
