"""
What each Gemini model actually is, and where it actually lives.

The layer below both `services/vertex.py` (which needs to know *where* a model
can be called) and `services/ai/model_router.py` (which needs to know *how hard*
it should think). Neither is the right owner: the transport must not import the
hotel agent package, and the router must not grow knowledge of Vertex regions.
So the facts live here once and both read them.

**Every location list in this file was measured, not assumed.** A `generateContent`
call was made for each model in each region of the configured universe; the
results are the table below. That matters more than it sounds, because the
failure this module exists to prevent is silent: Vertex answers a model/region
mismatch with `404 NOT_FOUND`, which is indistinguishable from a typo and was
read for months as "the project has no access to Gemini 3" when in fact the
whole family was reachable from `global` the entire time.

The three shapes that came out of the measurement:

    gemini-2.5-*      broadly regional. The rotation across independent
                      per-region quota pools works as designed.
    gemini-3.5-flash  four locations, including asia-south1 — our own compute
                      region, and the fastest of them at ~1.8s.
    everything else   `global` only. One quota pool, so rotating "global to
                      global" is not a fallback, it is the same call again.
      in 3.x          `vertex.py` reads `locations` to tell those two cases
                      apart; see `_post_generate`.
"""
from dataclasses import dataclass, field

# ── locations ────────────────────────────────────────────────────────────────
#
# `global` is not a region and does not get a region prefix on its hostname.
# That one asymmetry is the whole of the endpoint bug this catalogue was written
# alongside — see `vertex.get_vertex_host`.

GLOBAL = "global"

# Ours. Named because it is the tie-breaker: where a model is served from
# asia-south1, that is the one to prefer, because the application is there too.
HOME = "asia-south1"


# ── thinking ─────────────────────────────────────────────────────────────────
#
# Gemini 3.x thinks by default and bills for it. Measured on one trivial
# extraction: 2.5-flash spent 161 tokens, 3.5-flash spent 483 and 3.7-flash 375.
# On a high-volume path that is a silent multiple of the invoice for reasoning
# nobody asked for, so the budget is declared per model rather than inherited.
#
#   0     off. The right answer for a classifier and for schema-bound extraction.
#   None  the model decides. The right answer when the deliberation is the point.
THINKING_OFF = 0
THINKING_MODEL_DECIDES = None


@dataclass(frozen=True)
class ModelConfig:
    """One model, and what the system is allowed to assume about it."""

    name: str

    # What this model is *for*. Free-form on purpose: the router owns tier
    # semantics and this is a label, not a second routing table.
    tier: str

    # Where `generateContent` actually answers, in preference order. Measured.
    # The first entry is tried first; `vertex.py` intersects this with the
    # configured universe so a deployment can still narrow it.
    locations: tuple[str, ...]

    # Gemini's thinking budget. See the constants above.
    thinking_budget: int | None = THINKING_MODEL_DECIDES

    # False for a preview model. Not a blocker — 3.1-pro-preview is deliberately
    # reachable — but it is why it is an escalation path and not a default.
    ga: bool = True

    # Whether this model may be selected on a production path at all.
    # `gemini-3.7-flash` is the reason this field exists: it is a real, callable,
    # GA model that answered 8 of 8 concurrent requests with RESOURCE_EXHAUSTED,
    # so it is catalogued (the id is correct and the day its quota lands it is a
    # one-line change) and refused (today it would be an outage).
    production: bool = False

    # Where to go when this model fails or is switched off. Chains, and the
    # chain must terminate — `fallback_chain` asserts that it does.
    fallback: str | None = None

    # Verified by live probe. A model that cannot do these is not a candidate for
    # the agent runtime, whatever else it is good at.
    supports_tools: bool = True
    supports_response_schema: bool = True

    notes: str = field(default="", compare=False)

    @property
    def global_only(self) -> bool:
        """
        Whether region rotation means anything for this model.

        The distinction `_post_generate` turns on: with one location there is one
        quota pool, and a 429 has to be waited out rather than routed around.
        """
        return tuple(self.locations) == (GLOBAL,)


# ── the table ────────────────────────────────────────────────────────────────
#
# Ordered the way the architecture reads: router, workhorse, brain, escalation,
# then the stable 2.5 floor everything falls back to.

_CATALOG: dict[str, ModelConfig] = {}


def _add(config: ModelConfig) -> ModelConfig:
    _CATALOG[config.name] = config
    return config


# ── Gemini 3.x ───────────────────────────────────────────────────────────────

_add(ModelConfig(
    name="gemini-3.5-flash-lite",
    tier="classify",
    locations=(GLOBAL,),
    thinking_budget=THINKING_OFF,
    ga=True,
    production=True,
    fallback="gemini-2.5-flash-lite",
    notes="The router. Cheapest thing that can read an intent; ~1.2s measured.",
))

_add(ModelConfig(
    name="gemini-3.5-flash",
    tier="fast",
    # The only 3.x model with real regional availability, and asia-south1 is
    # first because it is where this application runs: 1.8s there against 2.4s
    # from global. It is also the only 3.x model for which the existing
    # cross-region 429 rotation still does anything.
    locations=(HOME, "asia-southeast1", GLOBAL, "northamerica-northeast1"),
    thinking_budget=THINKING_OFF,
    ga=True,
    production=True,
    fallback="gemini-2.5-flash",
    notes="High-volume workhorse: FAQs, lookups, simple CRUD. Thinking off.",
))

_add(ModelConfig(
    name="gemini-3.6-flash",
    tier="agent",
    locations=(GLOBAL,),
    thinking_budget=THINKING_MODEL_DECIDES,
    ga=True,
    production=True,
    fallback="gemini-3.5-flash",
    notes="The agent brain. Thinking left on — tool selection is the job.",
))

_add(ModelConfig(
    name="gemini-3.1-pro-preview",
    tier="deep",
    locations=(GLOBAL,),
    thinking_budget=THINKING_MODEL_DECIDES,
    ga=False,
    production=True,
    fallback="gemini-3.6-flash",
    notes="Escalation only. Preview, and ~3.8s — never a default.",
))

_add(ModelConfig(
    name="gemini-3.7-flash",
    tier="agent",
    locations=(GLOBAL,),
    thinking_budget=THINKING_MODEL_DECIDES,
    ga=True,
    # Deliberately off. See `ModelConfig.production`.
    production=False,
    fallback="gemini-3.6-flash",
    notes="Quota-starved on this project: 8/8 concurrent RESOURCE_EXHAUSTED.",
))

# Catalogued for id correctness rather than for use. These are the two names the
# configuration used to get wrong — `gemini-3-flash` and `gemini-3.1-pro`, which
# do not exist and 404 exactly like a model without access would.
_add(ModelConfig(
    name="gemini-3.1-flash-lite",
    tier="classify",
    locations=(GLOBAL,),
    thinking_budget=THINKING_OFF,
    ga=True,
    production=False,
    fallback="gemini-3.5-flash-lite",
    notes="Superseded by 3.5-flash-lite. Kept so the correct id is on record.",
))

_add(ModelConfig(
    name="gemini-3-flash-preview",
    tier="agent",
    locations=(GLOBAL,),
    ga=False,
    production=False,
    fallback="gemini-3.6-flash",
    notes="The real id of what config once called 'gemini-3-flash'.",
))

# ── Gemini 2.5 — the stable floor ────────────────────────────────────────────
#
# Not legacy and not deprecated. This is what the traveller-side pipeline runs
# on today and what every 3.x tier falls back to, so its region lists are the
# load-bearing ones: the cross-region 429 rotation exists for these.

_add(ModelConfig(
    name="gemini-2.5-flash",
    tier="fast",
    # Every region measured, home first. The only 2.5 model with full coverage.
    locations=(
        HOME, "asia-southeast1", "us-central1", "us-east1", "us-east4",
        "us-east5", "us-west1", "us-west4", "us-south1",
        "northamerica-northeast1", "europe-west1", "europe-west4", GLOBAL,
    ),
    thinking_budget=THINKING_MODEL_DECIDES,
    ga=True,
    production=True,
    fallback=None,
    notes="The floor. Everything degrades to this and it degrades to nothing.",
))

_add(ModelConfig(
    name="gemini-2.5-flash-lite",
    tier="classify",
    # No asia-south1, no asia-southeast1, no northamerica-northeast1 — measured
    # 404 in all three. Sending it there would turn a fallback into an outage.
    locations=(
        "us-central1", "us-east1", "us-east4", "us-east5", "us-west1",
        "us-west4", "us-south1", "europe-west1", "europe-west4", GLOBAL,
    ),
    thinking_budget=THINKING_OFF,
    ga=True,
    production=True,
    fallback="gemini-2.5-flash",
))

_add(ModelConfig(
    name="gemini-2.5-pro",
    tier="deep",
    locations=(
        "us-central1", "us-east1", "us-east4", "us-east5", "us-west1",
        "us-west4", "us-south1", "northamerica-northeast1", "europe-west1",
        "europe-west4", GLOBAL,
    ),
    thinking_budget=THINKING_MODEL_DECIDES,
    ga=True,
    production=True,
    fallback="gemini-2.5-flash",
))


# ── reading it ───────────────────────────────────────────────────────────────

def get(model: str) -> ModelConfig | None:
    """
    The entry for a model, or None if nobody catalogued it.

    None is a normal answer, not an error. `agent_configs.model` lets a property
    pin an arbitrary model id, and the correct response to one we have never
    measured is to fall back to the configured behaviour rather than to refuse
    the hotel's turn.
    """
    return _CATALOG.get(model)


def known() -> tuple[str, ...]:
    return tuple(_CATALOG)


def is_production(model: str) -> bool:
    """
    Whether this model may serve a real request.

    An uncatalogued model is allowed: it is a deliberate per-property override,
    and this function's job is to stop *our* mistakes, not the operator's.
    """
    config = get(model)
    return True if config is None else config.production


def thinking_budget_for(model: str, default: int | None = None) -> int | None:
    """
    The model's declared thinking budget, or `default` if it has none.

    Read by the router, so no caller sets a thinking budget of its own — which
    is what keeps the cost decision in one place instead of in every call site
    that happens to know a model is expensive.
    """
    config = get(model)
    if config is None:
        return default
    return config.thinking_budget


def locations_for(model: str, configured: list[str] | tuple[str, ...]) -> list[str]:
    """
    Where to try this model, in order, narrowed to what the deployment allows.

    Two inputs because they answer different questions. The catalogue knows
    where a model is *served*; `configured` is where this deployment is *willing*
    to send traffic (data residency, an operator narrowing the blast radius).
    A location must satisfy both.

    An uncatalogued model gets `configured` unchanged — the behaviour before any
    of this existed, which is the right default for a model nobody measured.

    Never returns an empty list. If the intersection is empty the catalogue wins,
    because a model that can only be reached from `global` and a deployment that
    forgot to list `global` is a misconfiguration, and answering it with "no
    locations at all" turns that into a dead agent rather than a slow one.
    """
    config = get(model)
    if config is None:
        return list(configured)

    allowed = set(configured)
    ordered = [loc for loc in config.locations if loc in allowed]
    return ordered or list(config.locations)


def fallback_chain(model: str, *, include_self: bool = True) -> list[str]:
    """
    The deterministic degradation path for a model, most capable first.

    Walks `fallback` to its end, skipping anything switched off for production
    and refusing to revisit a model it has already returned. Both guards matter:
    the first is what stops a chain degrading *into* `gemini-3.7-flash`, and the
    second is what stops a mis-edited table from hanging the process.
    """
    chain: list[str] = []
    seen: set[str] = set()
    current: str | None = model

    while current and current not in seen:
        seen.add(current)
        config = get(current)
        if current == model:
            if include_self:
                chain.append(current)
        elif config is None or config.production:
            chain.append(current)
        current = config.fallback if config else None

    return chain
