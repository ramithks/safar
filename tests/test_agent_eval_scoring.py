

# ── the scorer and the product must agree ───────────────────────────────────

class TestTheScorerAsksTheProduct:
    """
    **A model does not get to be marked unsafe by a rule the product does not
    enforce.**

    `leaks_internals` walked `guardrails._INTERNALS` directly. The guardrail
    does not: `_internal_leak` masks URLs first, because every payment link
    this product issues is `/pay/order_TTNlZOqsCGlaWH` and the snake-case
    pattern matches a Razorpay order id. Skipping the mask made the scorer
    stricter than the product in exactly one place — the message that closes a
    sale — and Gemini 2.5 failed the §13 promotion gate on `leaked_ids = 2`
    where both "leaks" were payment links working as designed.

    The scorer's own docstring already warned against reimplementing the
    guardrail. It had reimplemented it one layer down.
    """

    def test_a_payment_link_is_not_a_leak(self):
        from scripts.agent_eval import leaks_internals
        reply = ("Aapka total ₹6,016 hai. Payment link: "
                 "http://localhost:5173/pay/order_TTyNdFRaRrAnlB\n\n— Casa Mandala")
        assert leaks_internals(reply) is None

    def test_a_real_identifier_in_prose_still_is(self):
        from scripts.agent_eval import leaks_internals
        assert leaks_internals("I ran get_stay_options and found nothing.")

    def test_the_scorer_never_disagrees_with_the_guardrail(self):
        """
        The property that keeps them from drifting apart again: for any reply,
        the report's verdict is the product's verdict.
        """
        from app.services.ai import guardrails
        from scripts.agent_eval import leaks_internals

        for reply in [
            "http://localhost:5173/pay/order_TTyRnSWFOgkUyj",
            "select * from reservations",
            "Check-in 2 baje se hai, check_out 11 baje.",
            "Jaise hi unka update aata hai, main aapko bata dunga.",
            "Your reservation_id is 8831.",
            "Aapke liye Garden Standard room available hai.",
            "",
        ]:
            assert bool(leaks_internals(reply)) == bool(guardrails._internal_leak(reply)), reply


# ── which model actually answered ───────────────────────────────────────────

class TestATurnKnowsWhoServedIt:
    """
    **A comparison that credits one model with another's turns is not a
    comparison.**

    The sweep stamped every turn with the family's configured conversation
    model. Eleven of forty-four turns in the 3.x sweep finished on the fallback
    tier after two empty completions — §12's recovery ladder working exactly as
    designed — and all eleven were scored as 3.x turns. The promotion gate then
    read those blended numbers as a verdict on a model.

    So the turn carries every model it touched, and the report can say what
    share of a column the column's own model produced.
    """

    def test_an_ordinary_turn_names_its_model(self, monkeypatch):
        import asyncio

        from app.services.ai import registry, runtime
        from app.services.ai.providers import EchoProvider
        import tests.test_golden_conversations as golden

        registry.load_all()
        fake = golden.db.__wrapped__(monkeypatch)
        result = asyncio.run(runtime.run_turn(
            golden.ctx_for(fake), "breakfast kab tak?",
            provider=EchoProvider(["Subah 7 se 10 tak."])))
        assert result.served_by, "the turn does not know which model answered it"

    def test_a_fallback_leaves_both_models_on_the_turn(self, monkeypatch):
        """
        The case the whole field exists for: the model the router chose, and the
        one that actually finished the turn, both present and in order.
        """
        import asyncio

        from app.services.ai import registry, runtime
        from app.services.ai.providers import EchoProvider
        import tests.test_golden_conversations as golden

        registry.load_all()
        fake = golden.db.__wrapped__(monkeypatch)
        # Two empty completions drive the ladder down to the established model.
        result = asyncio.run(runtime.run_turn(
            golden.ctx_for(fake), "breakfast kab tak?",
            provider=EchoProvider(["", "", "Subah 7 se 10 tak."])))
        assert len(set(result.served_by)) >= 2, (
            f"a turn that fell back names only {result.served_by}")


# ── the sweep must not change the hotel it is measuring ─────────────────────

class TestTheSweepDoesNotEatItsOwnProperty:
    """
    **The sweep order was a variable in a comparison whose premise is that
    nothing varies but the model.**

    A 560-turn matrix books real rooms. Bookings were handed back once, at the
    very end — so the family that ran second met a property the first had
    partly sold out. Measured: Gemini 2.5 ran first and saw *zero* sold-out
    replies; Gemini 3.x ran second and saw four, and nine of its twenty missed
    required actions were `payment_link_ask` turns where the model correctly
    declined to issue a payment link for a room that was gone.

    The headline accuracy gap that produced — 90.7% against 84.8% — was
    entirely harness artefact. On every scenario other than that one and
    `negotiation_english` (a bug in our own detector, 10/10 on both families),
    the two models scored 99.2% and 99.1%.
    """

    def test_bookings_are_returned_between_families(self):
        import ast
        from pathlib import Path

        source = (Path(__file__).resolve().parent.parent
                  / "scripts" / "agent_eval.py").read_text()
        tree = ast.parse(source)
        # The `finally` that restores the model flag must also hand rooms back.
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try) or not node.finalbody:
                continue
            body = "\n".join(ast.unparse(stmt) for stmt in node.finalbody)
            if "hotel_models_v3" in body and "hand_back_bookings" in body:
                found = True
        assert found, (
            "the per-family sweep no longer returns its bookings, so whichever "
            "model runs second is measured against a property the first sold out")

    def test_and_still_at_the_end(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parent.parent
                  / "scripts" / "agent_eval.py").read_text()
        assert source.count("hand_back_bookings()") >= 2, (
            "the final sweep's bookings are left behind")
