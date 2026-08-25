"""
A quarter of one column was the other model's work.

Eleven of forty-four turns in the 3.x sweep finished on the fallback tier after
two consecutive empty completions each — the recovery ladder doing its job — and
all eleven were counted as 3.x turns, because the only model on the result was
the one the router had chosen. The comparison read as a verdict on Gemini 3.x
and was partly a verdict on Gemini 2.5 performing 3.x's recovery.

So: three populations, never summed, and a turn with no record is UNKNOWN
rather than assumed clean.
"""
import asyncio

import pytest

from app.services.ai import fallback_stats as fs
from app.services.ai import registry, runtime
from app.services.ai.providers import EchoProvider
import tests.test_golden_conversations as golden

PRIMARY = "gemini-x-flash"
OTHER = "gemini-y-flash"


def record(**over) -> dict:
    base = {"configured": PRIMARY, "served_by": [PRIMARY], "finished_on": PRIMARY,
            "fallback_count": 0, "attempts": [], "fallback_reasons": []}
    base.update(over)
    return base


class TestWhichPopulation:

    def test_a_clean_turn_is_primary_only(self):
        assert fs.assign(record()) == fs.PRIMARY_ONLY

    def test_a_turn_that_degraded_mid_flight_is_not(self):
        assert fs.assign(record(
            served_by=[PRIMARY, OTHER], finished_on=OTHER, fallback_count=1,
        )) == fs.FALLBACK_USED

    def test_a_turn_the_primary_never_contributed_to_is_fallback_only(self):
        assert fs.assign(record(
            served_by=[OTHER], finished_on=OTHER, fallback_count=1,
        )) == fs.FALLBACK_ONLY

    def test_a_turn_that_fell_back_and_came_home_is_still_not_clean(self):
        """
        `fallback_count` is the authority, not the endpoint. A turn that cost
        two models' work is not a clean reading of either, whichever one
        happened to write the last sentence.
        """
        assert fs.assign(record(
            served_by=[PRIMARY, OTHER], finished_on=PRIMARY, fallback_count=1,
        )) == fs.FALLBACK_USED

    def test_a_turn_with_no_record_is_unknown(self):
        """
        Never folded into PRIMARY_ONLY. Turns written before the field existed
        are not evidence that no fallback happened, and treating absence as
        "primary" recreates the original error one layer up.
        """
        assert fs.assign(None) == fs.UNKNOWN
        assert fs.assign({}) == fs.UNKNOWN
        assert fs.assign(record(served_by=[])) == fs.UNKNOWN
        assert fs.assign(record(configured=None)) == fs.UNKNOWN


class TestTheReportCannotBlendThem:

    @pytest.fixture
    def turns(self):
        return (
            [{"model": record(), "ok": True} for _ in range(30)]
            + [{"model": record(served_by=[PRIMARY, OTHER], finished_on=OTHER,
                                fallback_count=1,
                                fallback_reasons=["empty_completion"],
                                attempts=[{"failure": "empty_completion",
                                           "model": PRIMARY, "method": "fallback",
                                           "error_class": "ReadTimeout",
                                           "latency_ms": 9100}])}
               for _ in range(11)]
            + [{"model": record(served_by=[OTHER], finished_on=OTHER,
                                fallback_count=2,
                                fallback_reasons=["empty_completion"])}
               for _ in range(3)]
        )

    def test_the_populations_are_counted_apart(self, turns):
        counts = fs.summarise(turns)["counts"]
        assert counts[fs.PRIMARY_ONLY] == 30
        assert counts[fs.FALLBACK_USED] == 11
        assert counts[fs.FALLBACK_ONLY] == 3

    def test_no_blended_accuracy_is_offered(self, turns):
        """
        A caller wanting one number has to pick a population and say which.
        That is the whole argument of the module, and a convenience key here
        would quietly undo it.
        """
        summary = fs.summarise(turns)
        assert "accuracy" not in summary
        assert set(summary["rates"]) == {*fs.POPULATIONS, fs.UNKNOWN}

    def test_an_empty_population_still_appears(self, turns):
        """
        "FALLBACK_ONLY: 0" and no line at all read very differently.
        """
        clean = [{"model": record()} for _ in range(5)]
        assert fs.split(clean).keys() >= set(fs.POPULATIONS)
        assert fs.summarise(clean)["counts"][fs.FALLBACK_ONLY] == 0

    def test_the_fallback_rate_has_a_real_denominator(self, turns):
        summary = fs.summarise(turns)
        assert summary["n"] == 44
        assert summary["turns_that_fell_back"] == 14

    def test_the_provider_error_classes_are_carried_up(self, turns):
        assert fs.summarise(turns)["provider_error_classes"] == {"ReadTimeout": 11}

    def test_the_reasons_are_too(self, turns):
        assert fs.summarise(turns)["fallback_reasons"]["empty_completion"] == 14

    def test_an_empty_sweep_does_not_divide_by_zero(self):
        assert fs.summarise([])["rates"][fs.PRIMARY_ONLY] == 0.0


# ── the record the runtime actually writes ──────────────────────────────────

class TestTheRuntimeWritesIt:
    """
    The field these statistics read has to exist on every turn, not only on the
    turns that went wrong — a fallback rate needs the turns that did not fall
    back as much as the ones that did.
    """

    def _run(self, monkeypatch, replies):
        registry.load_all()
        fake = golden.db.__wrapped__(monkeypatch)
        return fake, asyncio.run(runtime.run_turn(
            golden.ctx_for(fake), "breakfast kitne baje tak?",
            provider=EchoProvider(replies)))

    def test_an_ordinary_turn_carries_the_record(self, monkeypatch):
        _, result = self._run(monkeypatch, ["Subah 7 se 10 tak."])
        model = (result.requirements or {}).get("model")
        assert model, "the turn wrote no model record"
        assert model["configured"] and model["served_by"]
        assert model["fallback_count"] == 0

    def test_and_it_reads_as_primary_only(self, monkeypatch):
        _, result = self._run(monkeypatch, ["Subah 7 se 10 tak."])
        assert fs.assign(result.requirements["model"]) == fs.PRIMARY_ONLY

    def test_a_turn_that_climbed_the_ladder_says_so(self, monkeypatch):
        _, result = self._run(monkeypatch, ["", "", "Subah 7 se 10 tak."])
        assert fs.assign(result.requirements["model"]) != fs.PRIMARY_ONLY

    def test_the_record_reaches_the_row(self, monkeypatch):
        fake, _ = self._run(monkeypatch, ["Subah 7 se 10 tak."])
        row = fake.rows("agent_runs")[0]
        assert (row.get("required_actions") or {}).get("model")

    def test_the_attempts_array_is_bounded(self, monkeypatch):
        """
        A pathological turn must not write an unbounded array into a row every
        dashboard reads.
        """
        _, result = self._run(monkeypatch, [""] * 20 + ["ok"])
        assert len(result.requirements["model"]["attempts"]) <= 8
