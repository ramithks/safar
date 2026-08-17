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

**`.env.stayos.coolify` is the answer.** It sits beside this file, gitignored,
holding all 24 variables with production values already filled in — paste it
into Coolify's environment editor whole. Regenerate it if a secret rotates.

It has been booted with **nothing else present**: `.env` moved aside so no value
could be inherited from a file that will not exist in the container, and
`stayos_backend_check.py` scored **20/20** against it. That is what proves the
list is complete rather than merely plausible.

The rest of this section is what is in that file and why, for when it needs
changing.

### Changed from the marketplace service

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
| `GOOGLE_MAPS_API_KEY` | The Places client init is wrapped in a try/except and logs "non-fatal" on failure, and no PMS route touches it. A secret a service does not use should not be on it. |
| `GCS_BUCKET_NAME`, `INSTAGRAM_PROXY_URL` | The reels pipeline and its scraper — marketplace only. `INSTAGRAM_PROXY_URL` is not the Instagram inbox; that is `INSTAGRAM_ACCESS_TOKEN`. |
| `WHATSAPP_TEST_RECIPIENT`, `WHATSAPP_TEST_PHONE_NUMBER_ID` | Only `scripts/meta_smoke.py` reads them, and it sends a real message. |

> ⚠ Do not paste `.env.hotelos` into Coolify. It is a **development** file:
> `APP_ENV=development`, `EXPOSE_API_DOCS=true`, and an `ALLOWED_ORIGINS` that
> includes four localhost ports. Every one of those is wrong in production.

### The other direction: five variables the test suites own

Not a deployment concern — a warning against "tidying" the two files into one.
These five must **never** be exported in a shell that runs the E2E suites:

```
MESSAGING_INLINE_PROCESSING   WHATSAPP_APP_SECRET   WHATSAPP_VERIFY_TOKEN
INSTAGRAM_APP_SECRET          INSTAGRAM_VERIFY_TOKEN
```

Fourteen suites open with `os.environ.setdefault(...)` on them, which does
nothing when the variable already exists. Adding them to `.env.hotelos` cost
five gates at once — whatsapp, instagram, marketing, creator, pilot and
partner_migration — because each suite then signed its webhooks with the test
secret while the app verified with the real one, and the agent never replied
because inline processing was off. All six failures looked like product bugs.

The asymmetry that makes this easy to get wrong: the same names inside the
`.env` **file** are harmless. Pydantic reads that file without populating
`os.environ`, so `setdefault` still wins. Only *exporting* breaks it, which is
exactly what `set -a; . ./.env.hotelos` does.

Production sets all five, where no suite runs.

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

## Redis

**`REDIS_URL` must be set. There is no safe default.**

One URL is the Celery broker, the job store, the rate limiter's storage and the
messaging pipeline's lock. Unset, it falls back to `redis://localhost:6379/0`,
and nothing listens on localhost inside a container — which is how the first
StayOS deploy came up reporting

```
"redis": "error: Error 111 connecting to localhost:6379. Connection refused."
```

That reads like Redis is down. It meant the variable was never set. The health
check now says which of the two it is, and `app/main.py` logs the target at
boot, both with credentials stripped.

The health check is the lucky part. The same URL is `broker=` in
`workers/celery_app.py`, and a worker pointed at an unreachable broker consumes
nothing and complains about nothing — guest replies and campaigns would simply
stop, silently. Read the boot log:

```
Redis: redis://redis:6379/1              ← configured
REDIS_URL is not set — falling back to…  ← not
```

### The value

**Copy the marketplace service's `REDIS_URL` out of Coolify and change only the
trailing database number.** Do not retype it from memory and do not take it from
`coolify.env` — that file is a snapshot from June and predates the password:

```
marketplace   redis://:<password>@redis:6379/0
StayOS        redis://:<password>@redis:6379/1
                                            ↑ the only edit
```

**Database 1**, where the marketplace uses 0. Queue routing is what actually
keeps tasks apart; separate databases keep result keys and locks apart too.

The password matters and is easy to lose: Coolify's managed Redis sets
`requirepass`, so a URL without one connects, gets refused, and reports
`Authentication required.` — which sounds like a permissions problem in the app
rather than a missing credential in a URL. Everything downstream is fine with an
authenticated URL; the queue-isolation proof runs green against one.

`redis` is a Docker-network hostname, so it only resolves if this service is in
the **same Coolify project** as the Redis container.

Each failure mode says something different, on purpose:

| What the health check says | What it means |
| --- | --- |
| `REDIS_URL is not set, so this fell back to redis://localhost:6379/0` | the variable is missing |
| `connecting to redis://redis:6379/1 — … nodename nor servname provided` | set, but the hostname does not resolve from this container — move the service into the Redis resource's project, or use an address that does |
| `connecting to redis://redis:6379/1 — … Connection refused` | host resolves, nothing listening on that port |
| `connecting to redis://redis:6379/1 — Authentication required.` | reachable, but the URL carries **no password** — copy the marketplace's whole URL, not just its host |
| `connecting to redis://redis:6379/1 — invalid username-password pair` | wrong password |
| `ok` | done |

Note what the middle rows have in common: the URL in the message never contains
the password, so any of these is safe to paste into a chat. That is
`config.redis_target()` doing its job.

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
