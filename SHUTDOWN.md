# Shut down — 2026-09-14

This backend no longer runs anywhere. Both deployments of this repository were
retired to bring infrastructure cost to zero:

| Deployment | Hostname | Status |
| --- | --- | --- |
| Marketplace (planner, hotels API, reels, reco, marketplace) | `backend.safar.example.com` | shut down |
| StaySuite (PMS, WhatsApp + Instagram messaging, Razorpay) | `api.staysuite.example.com` | shut down |

## What was deleted

GCP project `safar-499007`: the `safar-backend` VM (Coolify, both
compose stacks, their Redis), its 50 GB boot disk, the static IP
`34.93.214.160`, the `safar-deploy-499007` and `safar-media-2` buckets,
and the Vertex AI / Places API keys. No backup was taken (owner's decision).

## What still exists and is free

- **Supabase `Safar` (`uabxtdhxbamuhthxnhid`)** — all travel data stays:
  `hotels` + `hotel_profiles`, `seo_destinations`, `seo_places`, `seo_routes`,
  `seo_route_options`, `seo_route_legs`, `seo_itineraries*`, `transport_options`.
  The `seo-etl-nightly` pg_cron job keeps running inside Postgres.
- **safar.example.com (Vercel)** reads that data directly with the anon key:
  `/routes`, `/destinations`, `/weekend-trips`, `/hotels`. Hotel reads rely on
  migration `public_hotel_catalog_read` (RLS read policy on slugged hotels +
  the `hotel_cities()` function) instead of `GET /api/v1/hotels`.
- **Supabase `Safar agency` (`rdlozqvuywtgsodunait`)** — StaySuite data, untouched.

## Not migrated

The AI trip planner, workspace/TripDoc, copilot, reel pipeline, live price
scrapers, recommendation engine, marketplace checkout and the whole StaySuite API
have no serverless replacement. Restoring any of them means redeploying this
repository (see `DEPLOYING_STAYSUITE.md` for StaySuite) and recreating the GCP
credentials it expects.
