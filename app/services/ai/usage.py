"""
Usage — what a turn consumed.

Slice 1 records; slice 7 charges. The distinction matters, and this file exists
now rather than later so that when the meter arrives there is nothing to
retro-fit: every run already carries its provider, model and token counts, and
every action already declares a `usage_type`.

**Nothing here reads a balance and nothing here refuses anything.** That is the
credit rule stated as code: a hotel with no credits still checks guests in,
still posts folios, still runs its night audit. Only AI and messaging degrade,
and the degrading happens at the runtime's entry point in slice 7 — not inside a
tool, and never inside a PMS service.

The shape below is the one `usage_events` will take: an idempotency key per
countable thing, so a replayed webhook or a retried turn cannot be charged
twice. It is computed here already, because a key invented at billing time is a
key that cannot cover the events that happened before it.
"""
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class TurnUsage:
    """One run's consumption, in the dimensions the rate card will price."""

    provider: str = ""
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    # usage_type -> count. `ai.conversation_turn` is the billable unit a hotel
    # understands; the per-tool counts are for the operator's own cost model.
    units: dict[str, int] = field(default_factory=dict)

    def add_tokens(self, tokens_in: int, tokens_out: int) -> None:
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out

    def count(self, usage_type: str, n: int = 1) -> None:
        self.units[usage_type] = self.units.get(usage_type, 0) + n


def idempotency_key(run_id: str, usage_type: str, seq: int | None = None) -> str:
    """
    One key per countable thing.

    Keyed on the run rather than on the clock, so a retry of the same turn
    reuses it. The seq disambiguates several billable actions inside one run
    without making them one event.
    """
    return f"{usage_type}:{run_id}" + (f":{seq}" if seq is not None else "")


def record(run_id: str, usage: TurnUsage) -> None:
    """
    Where `usage_events` rows will be written.

    A no-op with a log line rather than a `pass`, so that when the metering
    migration lands the call sites are already correct and only this body
    changes. Debug level: a hotel's own logs should not narrate its billing.
    """
    logger.debug(
        "agent run %s used %s/%s tokens=%d/%d units=%s",
        run_id, usage.provider, usage.model, usage.tokens_in, usage.tokens_out, usage.units,
    )
