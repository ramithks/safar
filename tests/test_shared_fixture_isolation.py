"""
Suites must not fight over the demo property's configuration.

**The number this file exists to prevent.** A post-deployment evaluation
reported negotiation accuracy of **7%** and a 90% handoff rate. That reads as a
catastrophic model regression, and it was very nearly published as one.

It was not a regression. `agents.autonomy_level` on the demo property had been
left at 2 by an earlier e2e suite; `get_quote` and `negotiate_rate` both sit at
autonomy floor 3; at level 2 they queue for approval rather than executing, so
the agent handed off — correctly, and exactly as designed. Every one of those
thirteen "failures" was the autonomy ladder working, measured against a
configuration nobody had recorded.

Six suites wrote to that one row and none of them put it back. Whichever ran
last decided what the property was, and every measurement taken afterwards was
a measurement of that suite rather than of the build.

§11 asks for isolated fixtures. The ideal shape is a per-suite agent so there is
nothing shared to fight over; that is a change across six suites which each
assert against the demo property's own configuration, and making it in the same
pass as a change to the agent's decision path would be two large changes at once
(rule 11). So the smaller one, asserted here: a suite **borrows** the level, the
borrow is registered, and it goes back on exit — crash included.
"""
import ast
import pathlib
import re

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"

# A write to the shared configuration row, in either spelling the suites use.
SETS_AUTONOMY = re.compile(
    r"""(?:update\s+public\.agents\s+set\s+autonomy_level"""
    r"""|table\(\s*["']agents["']\s*\)\s*\.\s*update\(\s*\{[^}]*autonomy_level)""",
    re.IGNORECASE | re.VERBOSE,
)

# The helper that records the level it found and restores it with atexit.
BORROWS = "borrow_autonomy"

# Files that may write the row directly, and why.
#
#   e2e_sweep            defines the helper; the write is the implementation.
#   agent_eval           has its own `prepare_property`, which refuses to touch
#                        a non-demo hotel, prints the level it found, and stamps
#                        the level into the result. That is the same contract
#                        with a louder report, and it is the evaluator — the one
#                        script whose whole output is a measurement.
#   replay_e2e           same paragraph, same reason.
#   sync_agent_tools     reports the level; never writes it.
#   provision_missing_agents  creates agents that do not exist yet, so there is
#                        no prior level to preserve.
EXEMPT = {
    "e2e_sweep.py", "agent_eval.py", "replay_e2e.py",
    "sync_agent_tools.py", "provision_missing_agents.py",
}


def suites() -> list[pathlib.Path]:
    return sorted(p for p in SCRIPTS.glob("*.py") if p.name not in EXEMPT)


@pytest.mark.parametrize("path", suites(), ids=lambda p: p.name)
def test_a_suite_that_changes_autonomy_gives_it_back(path):
    """
    The rule, over every script in the directory.

    A suite that needs a particular autonomy level is doing something
    reasonable — `frontdesk_e2e` asserts what level 2 refuses, and it cannot do
    that at level 3. What it may not do is leave the property that way for
    whatever runs next.
    """
    source = path.read_text()
    if not SETS_AUTONOMY.search(source):
        return
    assert BORROWS in source, (
        f"{path.name} writes agents.autonomy_level directly. Use "
        f"e2e_sweep.borrow_autonomy(db, PROPERTY, level, agent_type=...), which "
        f"records the level it found and restores it on exit — including when "
        f"the suite crashes, which is how the six that leaked leaked."
    )


@pytest.mark.parametrize("name", [
    "ai_e2e.py", "frontdesk_e2e.py", "marketing_e2e.py",
    "sales_e2e.py", "whatsapp_e2e.py", "payment_status_e2e.py",
    # The seventh, found by the rule above rather than by reading the
    # directory — which is the argument for having a rule at all.
    "vertex_e2e.py",
])
def test_the_suites_that_leaked_now_borrow(name):
    """
    Named individually rather than left to the rule above, because these are
    the specific history: each one set the level for its own assertions,
    correctly, and none of them put it back.
    """
    source = (SCRIPTS / name).read_text()
    assert BORROWS in source
    assert "import e2e_sweep" in source


def test_the_helper_restores_what_it_found_and_not_what_it_set():
    """
    A suite that borrows twice must restore to the level it *found*, not to its
    own first change. The second borrow must not overwrite the baseline.
    """
    import sys
    sys.path.insert(0, str(SCRIPTS))
    import e2e_sweep
    from tests.fake_supabase import FakeDb

    db = FakeDb()
    db.seed("agents", {"id": "a-1", "property_id": "p-1",
                       "agent_type": "guest", "autonomy_level": 3})
    e2e_sweep._BORROWED_AUTONOMY.clear()

    assert e2e_sweep.borrow_autonomy(db, "p-1", 2) == 3
    assert db.rows("agents")[0]["autonomy_level"] == 2
    # A second borrow, of a different level, in the same run.
    assert e2e_sweep.borrow_autonomy(db, "p-1", 1) == 2
    assert db.rows("agents")[0]["autonomy_level"] == 1

    e2e_sweep.restore_autonomy(db)
    assert db.rows("agents")[0]["autonomy_level"] == 3, (
        "restored to the level the second borrow found, not to the level the "
        "property was at when the suite started"
    )


def test_restoring_twice_is_harmless():
    """
    `atexit` fires the restore and a suite may also call it in its own teardown.
    A second restore that wrote a stale level would be the bug in reverse.
    """
    import sys
    sys.path.insert(0, str(SCRIPTS))
    import e2e_sweep
    from tests.fake_supabase import FakeDb

    db = FakeDb()
    db.seed("agents", {"id": "a-1", "property_id": "p-1",
                       "agent_type": "guest", "autonomy_level": 3})
    e2e_sweep._BORROWED_AUTONOMY.clear()

    e2e_sweep.borrow_autonomy(db, "p-1", 2)
    assert e2e_sweep.restore_autonomy(db) == 1
    db.table("agents").update({"autonomy_level": 4}).eq("id", "a-1").execute()
    assert e2e_sweep.restore_autonomy(db) == 0
    assert db.rows("agents")[0]["autonomy_level"] == 4


def test_a_property_with_no_such_agent_is_not_invented():
    import sys
    sys.path.insert(0, str(SCRIPTS))
    import e2e_sweep
    from tests.fake_supabase import FakeDb

    e2e_sweep._BORROWED_AUTONOMY.clear()
    assert e2e_sweep.borrow_autonomy(FakeDb(), "p-1", 2) is None


# ── what every measurement has to carry ─────────────────────────────────────

def test_the_snapshot_records_everything_the_number_depends_on():
    """
    §11: *every evaluator must record hotel_id, agent_id, autonomy_level, model
    and a configuration snapshot with the evaluation result.*

    A number without these is not reproducible, and the 7% is what that looks
    like: a real measurement of a configuration nobody wrote down, which is
    indistinguishable from a measurement of the build.
    """
    import sys
    sys.path.insert(0, str(SCRIPTS))
    import e2e_sweep
    from tests.fake_supabase import FakeDb

    db = FakeDb()
    db.seed("properties", {"id": "p-1", "name": "Casa Mandala", "is_demo": True})
    db.seed("agents", {"id": "a-1", "property_id": "p-1", "agent_type": "guest",
                       "autonomy_level": 3, "status": "active",
                       "config_id": "cfg-9"})

    snapshot = e2e_sweep.configuration_snapshot(db, "p-1")

    assert snapshot["hotel_id"] == "p-1"
    assert snapshot["agent_id"] == "a-1"
    assert snapshot["autonomy_level"] == 3
    assert snapshot["config_id"] == "cfg-9"
    assert snapshot["is_demo"] is True
    # Which model actually answered is half of what a comparison is about.
    assert set(snapshot["models"]) >= {"conversation", "reasoning"}
    assert all(snapshot["models"].values())
