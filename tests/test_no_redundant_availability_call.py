"""
A tool description that tells the model to waste a round trip.

**Found by profiling production, not by reading.** Thirty days of Vertex
traffic, the commonest multi-tool sequence:

    49×  14.0s   search_availability → get_stay_options

Against 38 turns that called `get_stay_options` alone. So of the 87 turns that
reached it, 56% paid for a preceding call — and the two tools take the *same
arguments* and go to the *same* `availability_service.stay_options`, with
`get_stay_options` additionally resolving rate plans. Its result already carries
`unitsLeft`. The first call adds nothing.

**The cause was in the tool's own description**, which read *"Use after
search_availability when the guest asks what the options are."* The catalogue
was instructing the model to make the redundant call, and it obeyed.

What it costs, from the same profile: each extra tool call is another model step
that re-sends the whole catalogue, worth about 5,000 input tokens and 3.5
seconds. Across those 49 runs that is ~245,000 tokens and roughly three minutes
of guests waiting, for information the next call already contained.

This is a tool-selection matter, so a tool description is the right place for it
— not a line in the system prompt. It is the sentence the model reads at the
moment it chooses.
"""
import pytest

from app.services.ai import registry

registry.load_all()


def description(name: str) -> str:
    return registry.REGISTRY[name].description


class TestTheCatalogueDoesNotAskForARedundantCall:
    def test_stay_options_no_longer_says_to_call_availability_first(self):
        text = description("get_stay_options").lower()
        assert "after search_availability" not in text
        assert "search_availability" in text, (
            "it should still say something about the other tool — silence would "
            "leave the model to guess again"
        )
        assert "do not call search_availability" in text

    def test_stay_options_says_it_already_has_availability(self):
        assert "includes availability" in description("get_stay_options").lower()

    def test_search_availability_points_at_the_fuller_tool(self):
        text = description("search_availability").lower()
        assert "get_stay_options" in text
        assert "returns everything this does" in text

    def test_neither_description_forbids_the_standalone_use(self):
        """
        `search_availability` is still the right tool for "do you have anything
        next weekend". Removing it, or telling the model never to use it, would
        trade one wasted call for a worse answer to the commonest question a
        hotel gets.
        """
        assert "do you have anything" in description("search_availability").lower()

    def test_the_two_take_the_same_arguments(self):
        """
        The premise of the whole change. If they ever stop being
        interchangeable, the advice above becomes wrong.
        """
        a = registry.REGISTRY["search_availability"].input_model
        b = registry.REGISTRY["get_stay_options"].input_model
        assert set(a.model_fields) == set(b.model_fields)

    def test_the_fuller_tool_really_does_return_what_the_other_does(self):
        """
        Asserted against the source, because the claim in the description is
        only safe while it is true.
        """
        import inspect

        from app.services.ai.tools import read

        options = inspect.getsource(read.get_stay_options)
        assert '"unitsLeft"' in options, (
            "get_stay_options stopped reporting how many rooms are left, so it "
            "no longer subsumes search_availability"
        )

    def test_the_availability_result_tells_the_model_not_to_repeat_itself(self):
        """
        The instruction on the result, which is the second place the model
        decides — the first being the catalogue.
        """
        import inspect

        from app.services.ai.tools import read

        # Whitespace-normalised: the sentence is wrapped across source lines,
        # and a raw substring check would be asserting the formatter.
        source = " ".join(inspect.getsource(read.search_availability).lower().split())
        assert "do not call this again" in source
        assert "call get_stay_options" in source
