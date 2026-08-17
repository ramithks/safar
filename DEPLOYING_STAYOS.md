# Deploying the StayOS backend

This repository is deployed **twice**. Same code, same branch, two Coolify
services, two databases:

| Service | Hostname | `SUPABASE_URL` | `ENABLE_HOTEL_OS` | Serves |
| --- | --- | --- | --- | --- |
| Marketplace *(exists)* | `backend.travbuddy.in` | `uabxtdhxbamuhthxnhid` | unset → **false** | travbuddy.in, the apps, the planner |
| StayOS *(to create)* | `stayos-backend.travbuddy.in` | `rdlozqvuywtgsodunait` | **true** | partner.travbuddy.in |

Neither may point at the other's project. One process builds one Supabase
client from one `SUPABASE_URL` (`app/db/client.py`), so "which database" is a
property of the deployment, not of the request.

**Do not change the marketplace service.** Everything here creates a second one.

## Why it cannot just be one service

`/api/v1/pms/*` reads `properties`, `reservations`, `hotel_websites`. The
marketplace project has none of them, so mounted there every PMS route answered
500 — including `/public/hotels/{slug}`, which takes no login, so the errors
were public. `ENABLE_HOTEL_OS` now gates those routers and defaults to false,
which is why the marketplace is correct today with nothing set.

Authentication settles it on its own. The hotel OS issues **ES256** tokens and
`app/deps/auth.py` fetches the signing key from
`{SUPABASE_URL}/auth/v1/.well-known/jwks.json` — the project named by *this*
service's configuration. A backend pointed at the marketplace cannot verify a
StayOS token no matter what else is set.

## Environment

Start from the marketplace service's variables — they are the proven production
set — then apply the table below. **`GOOGLE_SERVICE_ACCOUNT_JSON`,
`GOOGLE_MAPS_API_KEY`, `GCP_*`, `GCS_BUCKET_NAME`, `SENTRY_DSN` and the
`RAZORPAY_*` trio are copied unchanged**: they are not database-scoped, and
Vertex, Maps and Razorpay-test are the same accounts for both products.

### Changed

| Variable | Value | |
| --- | --- | --- |
| `SUPABASE_URL` | `https://rdlozqvuywtgsodunait.supabase.co` | |
| `SUPABASE_SERVICE_KEY` | the **hotel-OS** service key | Supabase dashboard → Project Settings → API |
| `SUPABASE_ANON_KEY` | the **hotel-OS** anon key | same page |
| `ENABLE_HOTEL_OS` | `true` | mounts `/api/v1/pms/*` and the messaging webhooks |
| `APP_ENV` | `production` | |
| `ALLOWED_ORIGINS` | `https://partner.travbuddy.in` | the only browser origin that calls this service |
| `REDIS_URL` | the marketplace's, **with a different database number** — `…/1` where the marketplace uses `…/0` | see *Celery* below |

### Set explicitly to off

| Variable | Value | Why |
| --- | --- | --- |
| `EXPOSE_API_DOCS` | `false` | **`.env.hotelos` sets it to `true`** for local work. It publishes a complete map of every PMS route, parameter and model. The setting defaults to false, but say it anyway — see below. |
| `MESSAGING_INLINE_PROCESSING` | `false` | Development and the E2E suite only. In production Meta must be acknowledged fast and the model call must happen in Celery. |

> Belt and braces, and worth the two lines. `app/config.py` is a
> pydantic-settings model that reads the `.env` **file** as well as the
> environment, so locally `unset EXPOSE_API_DOCS` does not turn docs off — the
> file still says true. Coolify has no `.env`, so the default would apply, but
> an explicit `false` is right whether or not a file ever appears beside the
> code. Verified: with it set, `stayos_backend_check.py` goes 20/20; without it
> and with `.env` present, `/docs` answers 200.

### Must NOT be set at all

| Variable | Why |
| --- | --- |
| `SUPABASE_JWT_SECRET` | The marketplace's HS256 secret. StayOS tokens are ES256 and verify through JWKS. Copying it means an HS256 token would be checked against the wrong key; leaving it unset makes `app/deps/auth.py` refuse one loudly. |
| `WHATSAPP_TEST_RECIPIENT`, `WHATSAPP_TEST_PHONE_NUMBER_ID` | Only `scripts/meta_smoke.py` reads them, and it sends a real message. |

> ⚠ Do not paste `.env.hotelos` into Coolify. It is a **development** file:
> `APP_ENV=development`, `EXPOSE_API_DOCS=true`, and an `ALLOWED_ORIGINS` that
> includes four localhost ports. Every one of those is wrong in production.

### Meta credentials

`WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN` and the
`INSTAGRAM_*` set belong on **this** service, not the marketplace one — the
webhooks are mounted here. They are currently expired/absent, and WhatsApp and
Instagram stay unverified until they are real. Nothing else is blocked by that.

When they are, the callback URLs to register with Meta are on this hostname:

```
https://stayos-backend.travbuddy.in/api/v1/messaging/whatsapp/webhook
https://stayos-backend.travbuddy.in/api/v1/messaging/instagram/webhook
```

Both answer the `GET` verification handshake and take the `POST` deliveries, and
both verify `X-Hub-Signature-256` against `*_APP_SECRET` before reading a body.

## Celery

Two deployments enqueue through Celery. Without routing every task lands on one
default queue that both workers read, and whichever grabs it first runs it
against its own database.

Routing is in `app/workers/celery_app.py` and is deliberately one-sided:

- `travbuddy.messaging.*` and `travbuddy.marketing.*` → queue **`hotelos`**
- everything else stays on **`celery`**, because the marketplace worker is
  already running with no `-Q` and moving its tasks would stop reels, reco and
  partner commissions on the next deploy

So:

```sh
# StayOS worker — this queue only
celery -A app.workers.celery_app worker -l info -Q hotelos

# StayOS beat — do not run one.
```

Every entry in `beat_schedule` is a marketplace task, and
`partners.revert_intro_commission` fires **hourly** and writes to `partners` —
a table that also exists, meaning something entirely different, in the hotel OS.
Beat belongs to the marketplace service alone.

Give the StayOS service its own Redis database number anyway. The queue routing
is what makes crossover impossible; a separate namespace means the two do not
share result keys or locks either.

## Health check

`GET /api/v1/health` — returns `{"status":"healthy","dependencies":{…}}` and
checks Supabase and Redis. Point Coolify's health check there. There is no
`/healthz` on the backend; that path belongs to the PWA's nginx image.

## Verifying it

```sh
./venv/bin/python scripts/stayos_backend_check.py \
    --base https://stayos-backend.travbuddy.in \
    --anon-key <hotel-OS anon key>
```

Twenty checks. The one that matters is section 5: it signs in with a hotel-OS
account and reads a **property id back through the API**. A uuid exists in one
Supabase project, so if it comes back, the auth keys, the service key and the
schema are all the hotel OS's — which no amount of copied configuration can
fake. The script deliberately fails against `backend.travbuddy.in`; a check
that cannot fail is not a check.

Expect `/docs` to be the only failure if `EXPOSE_API_DOCS` was copied. Fix the
variable, do not silence the check.

## Then, the PWA

`VITE_API_BASE_URL` on the Vercel project `stayos-partner` must be this
service's origin, and **`VITE_*` is compiled into the bundle at build time** —
changing it in the Vercel panel does nothing until a redeploy. See
`Travbuddy_supply_os/DEPLOYING.md`.
