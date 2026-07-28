"""
GET  /api/v1/metrics/scrapers        — raw scraper observability snapshot.
GET  /api/v1/metrics/providers       — combined provider health (breaker + HTTP
                                        + generation queue): the "who's causing
                                        problems" view (Booking 92%, Vertex 98%…).
GET  /api/v1/metrics/dashboard        — self-contained HTML dashboard (auto-refresh).
POST /api/v1/metrics/scrapers/reset   — clear counters (e.g. before a load test).

Admin-only, all of it.

These were left open like /health on the reasoning that they expose no user
data. They expose something else: which providers we buy inventory from, our
concurrency ceilings, our p95s and which of our suppliers is currently failing —
an operational map of the business, served to anyone who asks. The reset
endpoint was worse than that; it was an unauthenticated POST that let a stranger
wipe the counters we'd use to notice an incident.

`require_admin` fails closed — with no passcode configured these answer 503
rather than opening up. Note that means the HTML dashboard no longer opens in a
plain browser tab; fetch it with the `X-Admin-Passcode` header (curl, or the
admin dashboard app).
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.core.circuit_breaker import ALL_BREAKERS
from app.core.limiter import limiter
from app.core.metrics import METRICS
from app.core import generation_queue
from app.deps.admin import AdminIdentity, require_admin

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("/scrapers")
@limiter.limit("120/minute")
async def scraper_metrics(
    request: Request,
    _admin: AdminIdentity = Depends(require_admin),
):
    return METRICS.snapshot()


@router.post("/scrapers/reset")
@limiter.limit("30/minute")
async def reset_scraper_metrics(
    request: Request,
    _admin: AdminIdentity = Depends(require_admin),
):
    METRICS.reset()
    return {"status": "reset"}


def _providers_view() -> dict:
    """Merge per-provider circuit-breaker stats (operation-level: success %,
    circuit state, in-flight/concurrency) with the scraper HTTP metrics
    (latency, block %, result counts), keyed by lowercase provider name."""
    http = METRICS.snapshot()["providers"]  # keys: 'booking', 'easemytrip', ...
    providers: dict[str, dict] = {}
    for br in ALL_BREAKERS:
        s = br.stats()
        key = s["name"].lower()
        h = http.get(key) or http.get(s["name"]) or {}
        providers[s["name"]] = {
            "success_pct": s["success_pct"],
            "state": s["state"],
            "in_flight": s["in_flight"],
            "max_concurrency": s["max_concurrency"],
            "peak_in_flight": s["peak_in_flight"],
            "timeout_sec": s["timeout_sec"],
            "calls": s["calls"],
            "successes": s["successes"],
            "failures": s["failures"],
            "timeouts": s["timeouts"],
            "short_circuited": s["short_circuited"],
            # HTTP-level detail (may be empty for non-scraper providers like Gemini)
            "http_failure_pct": h.get("failure_pct"),
            "blocked_pct": h.get("blocked_pct"),
            "latency_ms": h.get("latency_ms"),
            "avg_result_count": h.get("avg_result_count"),
        }
    return providers


@router.get("/providers")
@limiter.limit("120/minute")
async def provider_health(
    request: Request,
    _admin: AdminIdentity = Depends(require_admin),
):
    return {
        "generation_queue": generation_queue.stats(),
        "providers": _providers_view(),
    }


_ROW = """<tr class="{cls}">
  <td class="name">{name}</td>
  <td class="pct">{success}</td>
  <td>{state}</td>
  <td>{in_flight}/{max_c}</td>
  <td>{calls}</td>
  <td>{failures}</td>
  <td>{timeouts}</td>
  <td>{short}</td>
  <td>{latency}</td>
  <td>{results}</td>
</tr>"""


@router.get("/dashboard", response_class=HTMLResponse)
@limiter.limit("120/minute")
async def provider_dashboard(
    request: Request,
    _admin: AdminIdentity = Depends(require_admin),
):
    q = generation_queue.stats()
    rows = []
    for name, p in sorted(_providers_view().items()):
        sp = p["success_pct"]
        cls = "ok" if sp >= 90 else ("warn" if sp >= 70 else "bad")
        if p["state"] == "open":
            cls = "bad"
        lat = (p.get("latency_ms") or {}).get("p95")
        rows.append(_ROW.format(
            cls=cls, name=name,
            success=f"{sp:.0f}%",
            state=p["state"],
            in_flight=p["in_flight"], max_c=p["max_concurrency"],
            calls=p["calls"], failures=p["failures"], timeouts=p["timeouts"],
            short=p["short_circuited"],
            latency=f"{lat:.0f}ms" if lat else "—",
            results=p.get("avg_result_count") if p.get("avg_result_count") is not None else "—",
        ))
    html = _DASHBOARD_HTML.format(
        cap=q["capacity"], inflight=q["in_flight"], waiting=q["waiting"],
        peak=q["peak_in_flight"], queued=q["total_queued"], admitted=q["total_admitted"],
        rows="\n".join(rows),
    )
    return HTMLResponse(html)


_DASHBOARD_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Provider Health</title>
<meta http-equiv="refresh" content="5">
<style>
  body{{font:14px -apple-system,Segoe UI,Roboto,sans-serif;margin:24px;background:#0f1115;color:#e6e6e6}}
  h1{{font-size:18px;margin:0 0 4px}} .sub{{color:#8a8f98;margin-bottom:18px}}
  .queue{{display:flex;gap:20px;margin:0 0 20px;flex-wrap:wrap}}
  .card{{background:#191c22;border:1px solid #262a31;border-radius:10px;padding:12px 16px;min-width:120px}}
  .card .k{{color:#8a8f98;font-size:12px}} .card .v{{font-size:22px;font-weight:600;margin-top:2px}}
  table{{border-collapse:collapse;width:100%;background:#191c22;border-radius:10px;overflow:hidden}}
  th,td{{padding:9px 12px;text-align:right;border-bottom:1px solid #23262d}}
  th{{background:#20242b;color:#9aa0aa;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
  td.name{{text-align:left;font-weight:600}} td.pct{{font-weight:700;font-size:15px}}
  tr.ok td.pct{{color:#3fb950}} tr.warn td.pct{{color:#d29922}} tr.bad td.pct{{color:#f85149}}
  tr.bad{{background:rgba(248,81,73,.06)}}
  .foot{{color:#565b64;margin-top:14px;font-size:12px}}
</style></head><body>
<h1>Provider Health</h1>
<div class="sub">Auto-refreshes every 5s · success % is over completed provider calls</div>
<div class="queue">
  <div class="card"><div class="k">Gen queue capacity</div><div class="v">{cap}</div></div>
  <div class="card"><div class="k">In flight</div><div class="v">{inflight}</div></div>
  <div class="card"><div class="k">Waiting</div><div class="v">{waiting}</div></div>
  <div class="card"><div class="k">Peak in-flight</div><div class="v">{peak}</div></div>
  <div class="card"><div class="k">Total queued</div><div class="v">{queued}</div></div>
  <div class="card"><div class="k">Total admitted</div><div class="v">{admitted}</div></div>
</div>
<table>
<thead><tr>
  <th style="text-align:left">Provider</th><th>Success</th><th>Circuit</th><th>In-flight</th>
  <th>Calls</th><th>Fails</th><th>Timeouts</th><th>Skipped</th><th>p95</th><th>Avg results</th>
</tr></thead>
<tbody>
{rows}
</tbody></table>
<div class="foot">In-flight = current / max concurrency · Skipped = calls short-circuited while the breaker was open</div>
</body></html>"""
