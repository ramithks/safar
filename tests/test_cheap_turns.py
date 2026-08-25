"""
What the commonest messages in the system cost.

Three populations, measured rather than assumed:

    a greeting          "hi", "thanks"            — asks for nothing
    an informational    "what time is breakfast?" — asks for a fact
    everything else     "kitna hua?"              — asks for business

The first was fixed a pass ago: `is_opening` routes it without a model and
narrows to CORE. The second is the larger population and still paid for a
classify call — about 2.3 seconds, measured on the greeting path before that
one was cut, plus its tokens — to be told something `intent.category` already
knew deterministically and for free.

The numbers this file pins, per turn:

    tool schemas described     the catalogue is ~70% of the base prompt and
                               every model step re-sends it
    model calls                a classify call is a whole round trip
    routing source             a deterministic route must never be counted as
                               a model decision

The third is not a performance number and is here anyway: the report's "the
model chose it unprompted" figure is the one that would quietly inflate if a
shortcut reported itself as a model answer, and an optimisation that improves
a metric by lying about it is worse than no optimisation.
"""
import asyncio
import json

import pytest

from app.services.ai import classify, intent, model_router, registry, toolsets

GREETINGS = ["hi", "hello", "hey", "namaste", "thanks", "thank you", "shukriya"]

INFORMATIONAL = [
    "what time is breakfast?",
    "Breakfast kitne baje tak milta hai?",
    "wifi hai?",
    "What is the wifi password?",
    "checkout kab hai?",
    "checkout kitne baje hai?",
    "Parking free hai?",
    "Are pets allowed?",
    "Is the pool open?",
    "पूल कब तक खुला है?",
    "What is your cancellation policy?",
    "Do you have a gym?",
]

TRANSACTIONAL = [
    "5 kamre chahiye, 10 log aa rahe hain",
    "kitna hua?",
    "book kar do 20 se 22 October",
    "payment link bhej do",
]


@pytest.fixture(scope="module")
def granted():
    registry.load_all()
    return frozenset(registry.REGISTRY)


def schema_tokens(names) -> int:
    """
    Roughly what these schemas cost in the prompt. Four characters to a token
    is the standard approximation and is used consistently, so the comparisons
    below hold whatever the true tokeniser does.
    """
    described = []
    for name in sorted(names):
        spec = registry.REGISTRY.get(name)
        if spec:
            described.append({"name": spec.name, "description": spec.description,
                              "parameters": spec.input_model.model_json_schema()})
    return len(json.dumps(described)) // 4


def route(message: str, history=None):
    return asyncio.run(classify.ModelClassifier().classify(
        agent_type="guest", message=message, history=history))


class TestAGreetingCostsNothing:

    @pytest.mark.parametrize("message", GREETINGS)
    def test_no_model_decides_it(self, message):
        assert route(message).source == "default"

    @pytest.mark.parametrize("message", GREETINGS)
    def test_it_carries_the_core_catalogue(self, message, granted):
        routed = route(message)
        names = toolsets.for_task(routed.task, granted,
                                  opening=getattr(routed, "opening", False))
        assert schema_tokens(names) < 1200, (
            f"{message!r} described {len(names)} tools")


class TestAnInformationalQuestionCostsNothingToRoute:
    """
    The population this file was written for. Every one of these used to spend
    a classify call to learn what `intent.category` decides from the sentence.
    """

    @pytest.mark.parametrize("message", INFORMATIONAL)
    def test_no_model_decides_it(self, message):
        assert route(message).source == "default", (
            f"{message!r} still pays a classify call")

    @pytest.mark.parametrize("message", INFORMATIONAL)
    def test_it_routes_to_the_faq_catalogue(self, message):
        assert route(message).task == "guest_faq"

    @pytest.mark.parametrize("message", INFORMATIONAL)
    def test_which_is_a_fraction_of_the_whole(self, message, granted):
        narrow = schema_tokens(toolsets.for_task(route(message).task, granted))
        whole = schema_tokens(granted)
        assert narrow < whole / 4, (
            f"{message!r} described {narrow:,} of {whole:,} tokens")

    @pytest.mark.parametrize("message", INFORMATIONAL)
    def test_and_it_owes_no_business_action(self, message):
        """
        The two layers have to agree. If the requirement layer forced a tool on
        a turn the router narrowed away from it, the turn could not discharge
        what it owed — the optimisation and the correctness rule would be
        fighting.
        """
        from app.services.ai import required_actions

        assert required_actions.detect(message) == []


class TestTheShortcutCannotFireOnBusiness:

    @pytest.mark.parametrize("message", TRANSACTIONAL)
    def test_a_transactional_message_is_never_shortcut(self, message):
        """
        Checked on the category rather than by routing, for the reason in
        `test_ok_is_deliberately_not_narrowed`: these have no deterministic
        path, and reaching a live model to prove it is not what a unit test
        should do.
        """
        assert intent.category(message) != intent.INFORMATIONAL

    def test_an_unreadable_message_is_never_shortcut(self):
        """
        The safe default, checked here as well as in the intent tests because
        this is where getting it wrong under-equips a turn rather than merely
        mislabelling it.
        """
        for message in ("asdkjh", "??", "hmm", "achha"):
            assert intent.category(message) != intent.INFORMATIONAL

    def test_ok_is_deliberately_not_narrowed(self, granted):
        """
        "ok" can mean *yes, book it*. It is the one short message that must
        keep the whole catalogue, and a shortcut that swept it up with the
        greetings would silently disarm the turn a sale closes on.

        Asserted on the two decisions rather than by routing it: "ok" has no
        deterministic path, so `route()` would reach for a real model — which
        is the correct behaviour and the wrong thing for a test of it to do.
        """
        assert not classify.is_opening("ok")
        assert not classify.is_closing("ok")
        assert intent.category("ok") != intent.INFORMATIONAL
        # And the task it therefore falls to describes everything.
        assert len(toolsets.for_task("guest_conversation", granted)) > 50


class TestTheReportIsNotFlattered:

    @pytest.mark.parametrize("message", GREETINGS[:3] + INFORMATIONAL[:3])
    def test_a_deterministic_route_never_claims_a_model_made_it(self, message):
        """
        `source` feeds the evaluation's "the model chose it unprompted" figure.
        A shortcut reporting itself as a model decision would raise that number
        by removing model decisions from it, which is the worst kind of
        improvement.
        """
        assert route(message).source != "model"

    def test_the_tier_is_a_real_tier(self):
        for message in INFORMATIONAL[:4]:
            routed = route(message)
            assert routed.tier == model_router.tier_of(routed.task)


class TestTheNumbers:
    """
    The measurements themselves, as one table, so a regression shows up as a
    number moving rather than as an assertion nobody reads.
    """

    def test_the_three_populations_are_ordered_by_cost(self, granted):
        """
        The business row is priced from its task rather than by routing a
        message to it. A transactional message has no deterministic route — it
        goes to a real classify call, which is the behaviour under test
        everywhere else in this file and the wrong thing for a *measurement* of
        it to depend on: the number would come back over the network.
        """
        def cost(message):
            routed = route(message)
            return schema_tokens(toolsets.for_task(
                routed.task, granted, opening=getattr(routed, "opening", False)))

        greeting = max(cost(m) for m in GREETINGS)
        informational = max(cost(m) for m in INFORMATIONAL)
        business = max(schema_tokens(toolsets.for_task(task, granted))
                       for task in ("simple_quote", "guest_conversation"))

        assert greeting < informational < business, (
            f"greeting={greeting} informational={informational} business={business}")

    def test_an_informational_turn_is_under_three_thousand_tokens_of_schema(self, granted):
        for message in INFORMATIONAL:
            names = toolsets.for_task(route(message).task, granted)
            assert schema_tokens(names) < 3000, message

    def test_the_whole_catalogue_is_what_it_is_being_compared_to(self, granted):
        """
        A sanity check on the comparison itself: if the catalogue shrank to
        nothing, every ratio above would pass for the wrong reason.
        """
        assert schema_tokens(granted) > 10000
