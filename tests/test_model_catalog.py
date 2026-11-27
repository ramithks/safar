"""
Where a model lives, what it costs to think, and what a 404 actually means.

Every fact asserted here was measured against real Vertex on project
safar-499007 before it was written down — the catalogue is a record of
`generateContent` calls, not of documentation. The tests exist because the
failure mode is silent and expensive: a model asked for in a location that does
not serve it returns `404`, which is character-for-character what a model
without access returns, and reading one as the other cost this codebase months
of believing Gemini 3 was unavailable when the whole family was reachable.

No network here. The measurement is the input; this checks that the system draws
the right conclusions from it.
"""
import pytest

from app.config import settings
from app.services import model_catalog
from app.services.vertex import (
    VertexAccessDenied,
    VertexModelNotFound,
    VertexQuotaExhausted,
    VertexTimeout,
    VertexUpstreamError,
    classify_status,
    get_vertex_host,
    _regions,
)


# ── the endpoint ─────────────────────────────────────────────────────────────
#
# The bug at the centre of all of this. One asymmetry, one function.

def test_global_has_no_region_prefix():
    # `global-aiplatform.googleapis.com` does not resolve. Formatting the prefix
    # unconditionally is what made every 3.x model unreachable, and made the
    # resulting 404 look like a permissions problem.
    assert get_vertex_host("global") == "aiplatform.googleapis.com"


@pytest.mark.parametrize("location,host", [
    ("asia-south1", "asia-south1-aiplatform.googleapis.com"),
    ("asia-southeast1", "asia-southeast1-aiplatform.googleapis.com"),
    ("us-central1", "us-central1-aiplatform.googleapis.com"),
    ("europe-west4", "europe-west4-aiplatform.googleapis.com"),
])
def test_a_real_region_keeps_its_prefix(location, host):
    assert get_vertex_host(location) == host


def test_every_url_is_built_through_the_helper():
    # The property that stops the asymmetry being reintroduced by a second call
    # site that formats its own URL — which is exactly how it got in.
    # An *interpolated* prefix — `https://{something}-aiplatform...` — which is
    # the shape of the bug. Prose naming the hostname is not.
    import pathlib
    import re

    built = re.compile(r"https://\{[^}]+\}-aiplatform\.googleapis\.com")
    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if path.name != "vertex.py" and built.search(path.read_text())
    ]
    assert not offenders, f"hand-built Vertex hostnames: {offenders}"


# ── the location universe ────────────────────────────────────────────────────

def test_the_configured_universe_includes_global_and_home():
    # `global` because almost the whole 3.x family is served from there and
    # nowhere else. `asia-south1` because it is where this application runs, and
    # it was absent from a list of eleven regions on three continents.
    configured = _regions()
    assert "global" in configured
    assert "asia-south1" in configured


# ── where each model actually is ─────────────────────────────────────────────

def test_the_3x_family_is_global_only_except_the_workhorse():
    # Measured. This is the fact the retry policy turns on.
    for model in ("gemini-3.6-flash", "gemini-3.5-flash-lite",
                  "gemini-3.1-pro-preview", "gemini-3.7-flash"):
        assert model_catalog.get(model).global_only, model


def test_the_fast_model_prefers_our_own_region():
    # gemini-3.5-flash is the only 3.x model with real regional availability,
    # and asia-south1 measured 1.8s against global's 2.4s.
    config = model_catalog.get("gemini-3.5-flash")
    assert not config.global_only
    assert config.locations[0] == "asia-south1"
    assert "global" in config.locations


@pytest.mark.parametrize("model", ["gemini-2.5-pro", "gemini-2.5-flash-lite"])
def test_the_2_5_models_that_are_not_in_mumbai_are_not_sent_there(model):
    # Both measured 404 in asia-south1 and asia-southeast1. Adding asia-south1
    # to the universe without the catalogue would have sent them there and
    # turned the stable fallback into an outage.
    locations = model_catalog.locations_for(model, _regions())
    assert "asia-south1" not in locations
    assert "asia-southeast1" not in locations
    assert "us-central1" in locations


def test_the_floor_model_keeps_its_full_rotation():
    # gemini-2.5-flash is what everything degrades to, and its many regions are
    # what the cross-region 429 rotation was built for. Narrowing it would
    # weaken the thing the fallback depends on.
    locations = model_catalog.locations_for("gemini-2.5-flash", _regions())
    assert len(locations) >= 12


def test_a_model_nobody_catalogued_gets_the_configured_order_unchanged():
    # `agent_configs.model` lets a property pin an arbitrary id. The right
    # response to one we never measured is the behaviour from before any of this
    # existed, not a refusal of that hotel's turn.
    configured = ["us-central1", "europe-west4"]
    assert model_catalog.locations_for("gemini-invented", configured) == configured


def test_a_deployment_can_narrow_but_the_catalogue_still_wins_when_it_over_narrows():
    # Intersecting is right; returning nothing is not. A model reachable only
    # from global, in a deployment that forgot to list global, is a
    # misconfiguration — answering it with "nowhere at all" makes it a dead
    # agent instead of a slow one.
    assert model_catalog.locations_for("gemini-3.5-flash", ["global"]) == ["global"]
    assert model_catalog.locations_for("gemini-3.6-flash", ["us-central1"]) == ["global"]


# ── thinking, and what it costs ──────────────────────────────────────────────

def test_the_high_volume_tiers_do_not_think():
    # Measured: the same trivial extraction cost 161 tokens on 2.5-flash and 483
    # on 3.5-flash with thinking left at the model's default. Inheriting that
    # default would have tripled the bill on the busiest paths the moment the
    # ids changed, with nothing failing to show it.
    assert model_catalog.thinking_budget_for("gemini-3.5-flash") == 0
    assert model_catalog.thinking_budget_for("gemini-3.5-flash-lite") == 0


def test_the_brain_and_the_escalation_model_do_think():
    # None means "the model decides", which is the point of using it: choosing
    # between twenty tools is the job, not an overhead on it.
    assert model_catalog.thinking_budget_for("gemini-3.6-flash") is None
    assert model_catalog.thinking_budget_for("gemini-3.1-pro-preview") is None


def test_an_uncatalogued_model_falls_back_to_the_tier_default():
    assert model_catalog.thinking_budget_for("gemini-invented", default=0) == 0


# ── the ids that were wrong ──────────────────────────────────────────────────

def test_the_two_ids_that_do_not_exist_are_not_configured_anywhere():
    # `gemini-3-flash` and `gemini-3.1-pro` were in config.py as the "target"
    # models. Neither is a real id; both 404 exactly like a model without
    # access, which is what produced the wrong diagnosis.
    # Matched where an id is *assigned* rather than anywhere it appears, so the
    # catalogue and this file can still name them to explain what went wrong.
    import pathlib
    import re

    dead = re.compile(
        r"""(?x)
        (?: = \s* | name \s* = \s* )      # a setting, or a ModelConfig name
        ["'] gemini-3(?:-flash|\.1-pro) ["']   # and not the -preview suffix
        """
    )
    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if dead.search(path.read_text())
    ]
    assert not offenders, f"model ids that do not exist: {offenders}"


@pytest.mark.parametrize("model", [
    "gemini-3-flash-preview", "gemini-3.1-pro-preview", "gemini-3.1-flash-lite",
])
def test_the_real_preview_ids_are_on_record(model):
    assert model_catalog.get(model) is not None


def test_every_configured_tier_model_is_catalogued():
    # A tier pointing at a model nobody measured is a tier with no location
    # list, no thinking budget and no fallback.
    from app.services.ai import model_router

    for tier in model_router.tiers():
        assert model_catalog.get(model_router.model_for_tier(tier)) is not None, tier


# ── what may serve traffic ───────────────────────────────────────────────────

def test_the_quota_starved_model_is_refused():
    # gemini-3.7-flash is real, GA and callable, and answered 8 of 8 concurrent
    # probes with RESOURCE_EXHAUSTED. Configuring it would not fail loudly — it
    # would degrade every turn into a retry ladder.
    assert model_catalog.is_production("gemini-3.7-flash") is False


def test_no_fallback_chain_ever_lands_on_it():
    for model in model_catalog.known():
        assert "gemini-3.7-flash" not in model_catalog.fallback_chain(
            model, include_self=False
        )


def test_the_brain_degrades_to_the_model_that_is_already_in_production():
    # The property that matters about the ladder: it terminates somewhere
    # known-good rather than somewhere new.
    assert model_catalog.fallback_chain("gemini-3.6-flash") == [
        "gemini-3.6-flash", "gemini-3.5-flash", "gemini-2.5-flash",
    ]


def test_every_chain_terminates():
    for model in model_catalog.known():
        chain = model_catalog.fallback_chain(model)
        assert len(chain) == len(set(chain)), f"{model} loops: {chain}"
        assert chain[-1] is not None


# ── the rollback ─────────────────────────────────────────────────────────────

def test_one_variable_puts_every_tier_back_on_2_5(monkeypatch):
    # Not a big-bang migration: 3.x is the default because it is better, and it
    # is one environment variable away from not being.
    from app.services.ai import model_router

    monkeypatch.setattr(settings, "hotel_models_v3", False)
    served = {t: model_router.model_for_tier(t) for t in model_router.tiers()}
    assert all(m.startswith("gemini-2.5") for m in served.values()), served
    # And it restores what was actually in production, not an approximation.
    assert served[model_router.CONVERSATION] == "gemini-2.5-flash"
    assert served[model_router.DEEP] == "gemini-2.5-pro"


def test_with_the_flag_on_the_tiers_are_the_3x_architecture(monkeypatch):
    from app.services.ai import model_router

    monkeypatch.setattr(settings, "hotel_models_v3", True)
    assert model_router.model_for_tier(model_router.CLASSIFY) == "gemini-3.5-flash-lite"
    assert model_router.model_for_tier(model_router.FAST) == "gemini-3.5-flash"
    assert model_router.model_for_tier(model_router.CONVERSATION) == "gemini-3.6-flash"
    assert model_router.model_for_tier(model_router.DEEP) == "gemini-3.1-pro-preview"


# ── error classification ─────────────────────────────────────────────────────
#
# The distinction that was missing, and the reason a reachable model family was
# recorded as unavailable.

def test_a_404_is_a_model_problem_and_says_so():
    exc = classify_status(404, model="gemini-3.6-flash", location="us-central1")
    assert isinstance(exc, VertexModelNotFound)
    # The sentence an operator reads. It must not send them to IAM.
    assert "not an access problem" in str(exc)
    assert exc.retryable is False


def test_a_403_is_the_only_access_problem():
    exc = classify_status(403, model="gemini-3.6-flash")
    assert isinstance(exc, VertexAccessDenied)
    assert exc.retryable is False


def test_a_429_is_quota_and_is_retryable():
    exc = classify_status(429, model="gemini-3.6-flash", location="global")
    assert isinstance(exc, VertexQuotaExhausted)
    assert exc.retryable is True


@pytest.mark.parametrize("status", [500, 502, 503])
def test_5xx_is_upstream_and_is_retryable(status):
    exc = classify_status(status, model="gemini-3.5-flash")
    assert isinstance(exc, VertexUpstreamError)
    assert exc.retryable is True


def test_a_timeout_is_retryable_because_generate_is_a_read():
    assert VertexTimeout("slow").retryable is True


def test_the_detail_survives_classification():
    # `raise_for_status` throws the body away, and the body is where Vertex
    # explains itself. That cost an afternoon on a function-response parity bug
    # whose log said only "Client error '400 Bad Request'".
    exc = classify_status(400, "the number of function response parts must equal",
                          model="gemini-3.6-flash")
    assert "function response parts" in str(exc)


# ── the tier vocabulary the database will accept ─────────────────────────────

def test_every_router_tier_is_allowed_by_the_agent_runs_constraint():
    """
    The bug 2,052 passing tests did not catch.

    `agent_runs.model_tier` carries a CHECK constraint, `runtime._open_run`
    writes `choice.tier` into it as the first statement of every turn, and the
    test double has no constraints — so splitting the conversation tier passed
    CI and killed a plain FAQ turn on the real database, before the model was
    even called.

    Parsed out of the migration rather than restated here. A copy of the list
    would drift from the schema, which is the same failure one level up.
    """
    import pathlib
    import re

    migration = (
        pathlib.Path(__file__).resolve().parent.parent
        / "migrations" / "2026-08-24_a_lookup_is_not_a_negotiation.sql"
    ).read_text()

    allowed = set(re.findall(r"'([a-z_]+)'", migration.split("array[")[1].split("]")[0]))
    from app.services.ai import model_router

    assert set(model_router.tiers()) == allowed, (
        f"router tiers {set(model_router.tiers())} do not match what the "
        f"database will accept {allowed}"
    )
