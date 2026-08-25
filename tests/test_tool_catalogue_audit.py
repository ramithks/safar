"""
No tool may tell the model to make a call another tool already covers.

**Found by profiling, then generalised.** Thirty days of Vertex traffic had one
sequence above all others:

    49×  14.0 s   search_availability → get_stay_options
    38×  13.3 s   get_stay_options

56% of the turns that reached `get_stay_options` paid for a preceding call —
because its own description said *"Use after search_availability"*, and the two
take the same arguments, hit the same service, and the second already returns
`unitsLeft`. The catalogue was instructing the model to waste a round trip and
the model obeyed.

Each tool call is another model step that re-sends every schema: about 5,000
input tokens and 3.5 seconds, measured. So a redundant pair is not a tidiness
problem, it is a third of a guest's waiting time.

**This file is the audit rather than the fix**, because the fix is a sentence in
a description and the thing worth keeping is the rule. It checks three
properties mechanically, so the next tool somebody adds with overlapping
arguments has to say how it differs before it can ship.
"""
import inspect
import re

import pytest

from app.services.ai import agents, registry

registry.load_all()

READS = sorted((s for s in registry.REGISTRY.values() if not s.mutating),
               key=lambda s: s.name)
GUEST_FACING = frozenset(agents.get("guest").tools) | frozenset(agents.get("sales").tools)


def args_of(name: str) -> frozenset[str]:
    return frozenset(registry.REGISTRY[name].input_model.model_fields)


def _returned_fields(name: str) -> frozenset[str]:
    """The keys a tool's own source puts in its result."""
    source = inspect.getsource(registry.REGISTRY[name].fn)
    return frozenset(re.findall(r'"([a-zA-Z][a-zA-Z0-9]*)":', source))


def overlapping_pairs():
    """
    Read tools a model could genuinely reach for interchangeably.

    **Three conditions, and the first draft of this had only two**, which
    matched 182 pairs and was useless — every no-argument tool takes the same
    arguments as every other, so it flagged `list_arrivals` against
    `list_services`. Nobody confuses the arrivals list with the room-service
    menu, and a rule that shouts about those is a rule somebody deletes.

    What makes a pair a real choice is that they *answer the same question*, and
    the closest mechanical proxy for that is the shape of what they return:

      * the same arguments — the model has the same information either way;
      * on one agent's menu — otherwise the model never sees both;
      * and one result is close to a subset of the other, which is what "this
        tool already tells you that" means.

    Two thirds is the threshold, and `get_task` / `get_task_status` is why: the
    smaller returns `taskId`, `code`, `status`, `assignedTo`, `resolvedAt` and
    an instruction, and every one of those but the instruction is in the larger.
    """
    seen = []
    for i, first in enumerate(READS):
        for second in READS[i + 1:]:
            if args_of(first.name) != args_of(second.name):
                continue
            shared = next(
                (holder for holder in ("guest", "frontdesk", "owner", "marketing",
                                       "sales")
                 if {first.name, second.name} <= set(agents.get(holder).tools)),
                None,
            )
            if shared is None:
                continue
            a, b = _returned_fields(first.name), _returned_fields(second.name)
            smaller, larger = (a, b) if len(a) <= len(b) else (b, a)
            # **A floor on the overlap, not just a ratio.** The ratio alone
            # flagged every analytics tool against every other, because they
            # share a metrics envelope and several return a single key —
            # `{"signals"}` is trivially a subset of anything. Four common
            # fields is the point where two results are describing the same
            # thing rather than sharing a wrapper.
            common = smaller & larger
            if len(common) < 4:
                continue
            if len(common) / len(smaller) >= 2 / 3:
                seen.append((first.name, second.name, shared))
    return seen


class TestNoDescriptionAsksForARedundantCall:
    """
    The specific defect, stated as a rule. A description telling the model to
    call another tool *first* is the catalogue adding a round trip.
    """

    AFTER = re.compile(r"\b(?:use|call)\s+(?:this\s+)?after\s+(\w+)", re.I)
    FIRST = re.compile(r"\b(\w+)\s+first\b", re.I)
    # A negation before it turns the sentence inside out. `get_stay_options`
    # reads "do NOT call search_availability first", which is the fix rather
    # than the defect, and a rule that cannot tell those apart is a rule that
    # fails on its own remedy.
    NEGATED = re.compile(r"\b(?:do\s+not|don't|never|no\s+need\s+to)\b[^.]*$", re.I)

    # Prerequisites that are real: the named tool returns something this one
    # does not, and the model needs it to answer well.
    #
    # **A dict, so an exception cannot be added without a reason.** A bare set
    # of pairs is a list somebody appends to when a test goes red; a mapping to
    # prose is one they have to think about, and `test_every_exception_gives_a
    # _reason` below refuses an empty one. Suppressing the detector entirely is
    # the thing this shape exists to prevent.
    LEGITIMATE = {
        ("check_out", "get_folio"):
            "the checkout refuses while money is outstanding and returns no "
            "folio of its own, so without this the agent can say 'I can't "
            "check you out' and not say why — a second turn for the guest, "
            "which costs more than the call it saves",
    }

    @pytest.mark.parametrize("spec", registry.REGISTRY.values(), ids=lambda s: s.name)
    def test_it_does_not_tell_the_model_to_call_another_tool_first(self, spec):
        for pattern in (self.AFTER, self.FIRST):
            for match in pattern.finditer(spec.description):
                named = match.group(1)
                if named not in registry.REGISTRY:
                    continue
                if (spec.name, named) in self.LEGITIMATE:
                    continue
                clause = spec.description[:match.start()].rsplit(".", 1)[-1]
                if self.NEGATED.search(clause):
                    continue
                raise AssertionError(
                    f"{spec.name} tells the model to call {named} first. If "
                    f"{named} returns something this tool does not, add the "
                    f"pair to LEGITIMATE with the reason; if it does not, this "
                    f"is a wasted model step — about 5,000 input tokens and "
                    f"3.5 seconds, measured."
                )

    def test_the_pair_this_was_found_on_stays_fixed(self):
        stay = registry.REGISTRY["get_stay_options"].description.lower()
        assert "after search_availability" not in stay
        assert "do not call search_availability" in stay
        assert "includes availability" in stay

    def test_and_the_cheaper_tool_still_has_its_own_use(self):
        """
        `search_availability` is the right answer to "do you have anything next
        weekend". Removing it, or telling the model never to reach for it, would
        trade one wasted call for a worse answer to the commonest question a
        hotel gets.
        """
        assert "do you have anything" in \
            registry.REGISTRY["search_availability"].description.lower()


class TestToolsThatCompeteMustSayHowTheyDiffer:
    """
    Two reads with identical arguments on one agent's menu are a choice the
    model has to make every turn. It can only make it from the descriptions.
    """

    @pytest.mark.parametrize("pair", overlapping_pairs(),
                             ids=lambda p: f"{p[0]}~{p[1]}")
    def test_each_names_the_other(self, pair):
        first, second, holder = pair
        if (first, second) in self.ALLOWED_OVERLAP:
            pytest.skip(self.ALLOWED_OVERLAP[(first, second)])
        a = registry.REGISTRY[first].description
        b = registry.REGISTRY[second].description
        assert second in a and first in b, (
            f"{first} and {second} take the same arguments and are both on the "
            f"{holder} agent's menu, and neither says how it differs from the "
            f"other. The model is choosing between them blind, and calling both "
            f"costs a whole extra model step."
        )

    # Pairs that overlap and are allowed to, each with why.
    #
    # Same shape and same reason as LEGITIMATE above: an exception has to be
    # argued, not appended.
    ALLOWED_OVERLAP: dict = {}

    def test_every_exception_gives_a_reason(self):
        """
        The property that keeps this from becoming a mute button. An entry with
        no prose behind it is somebody silencing the detector, and this fails on
        it — including on an entry whose "reason" is a placeholder.
        """
        for table in (TestNoDescriptionAsksForARedundantCall.LEGITIMATE,
                      self.ALLOWED_OVERLAP):
            for pair, reason in table.items():
                assert isinstance(reason, str) and len(reason.split()) >= 8, (
                    f"{pair} is exempted without a reason anybody can weigh"
                )
                assert "todo" not in reason.lower() and "fixme" not in reason.lower()

    def test_the_detector_cannot_be_switched_off_wholesale(self):
        """
        §8: false positives must be representable as explicit exceptions, and
        the detector must not be suppressed. If the exception list ever grows to
        cover everything it finds, it has stopped being an audit.
        """
        found = {(a, b) for a, b, _ in overlapping_pairs()}
        exempted = set(self.ALLOWED_OVERLAP)
        assert not found <= exempted or not found, (
            "every overlapping pair is exempted — the detector is off in all "
            "but name"
        )

    def test_the_audit_actually_found_something(self):
        """
        A rule that matches nothing is a rule nobody can trust. `get_task` and
        `get_task_status` are the real pair: same permission, same argument,
        both on the guest agent, and `get_task` returns a superset of the
        other's fields.
        """
        pairs = {(a, b) for a, b, _ in overlapping_pairs()}
        assert ("get_task", "get_task_status") in pairs


class TestNoToolCallsAnotherToolsWork:
    """
    The other shape of redundancy: not the model making two calls, but one tool
    quietly doing another's job, so the model cannot tell what it has.
    """

    def test_the_two_availability_reads_go_to_the_same_service(self):
        """
        The premise of the description fix. If they ever stop being
        interchangeable, the advice in the catalogue becomes wrong.
        """
        from app.services.ai.tools import read

        for name in ("search_availability", "get_stay_options"):
            assert "availability_service.stay_options" in inspect.getsource(
                getattr(read, name))

    def test_the_fuller_one_still_reports_what_the_cheaper_one_does(self):
        from app.services.ai.tools import read

        assert '"unitsLeft"' in inspect.getsource(read.get_stay_options), (
            "get_stay_options stopped reporting how many rooms are left, so it "
            "no longer subsumes search_availability and the description lies"
        )
