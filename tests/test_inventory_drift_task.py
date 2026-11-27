"""
Inventory drift, noticed rather than discovered.

**The reconciliation existed and nothing ran it.** `scripts/reconcile_inventory`
has found real drift twice — 377 room-nights sold to nobody at Casa Mandala, and
eighteen units oversold from fixture rows INSERTed straight into `reservations`
that never moved the counter. Both were found because somebody happened to run
it by hand.

Drift is the one class of defect in this system that does not announce itself.
An oversold night sells a room the hotel does not have, and nobody learns until
two guests arrive for it. A stranded night quietly takes a sellable room off the
market and looks like a slow week.

**It reports and never corrects**, and that is the load-bearing decision.
Recomputing the counter from live reservations is right for a *report* and
dangerous for a write: a hold in flight, a webhook mid-delivery and a booking
being made in the same second all look like disagreement for a moment, and a
task that "fixed" them would take a room off a guest who is paying for it.
"""
import logging

import pytest

from app.workers import messaging_tasks
from tests.fake_supabase import FakeDb, install

A = "aaaaaaaa-0000-4000-8000-00000000000a"
B = "bbbbbbbb-0000-4000-8000-00000000000b"


@pytest.fixture
def db(monkeypatch) -> FakeDb:
    fake = FakeDb()
    fake.seed("properties",
              {"id": A, "name": "Casa Mandala", "status": "active"},
              {"id": B, "name": "Somewhere Else", "status": "active"},
              {"id": "paused", "name": "Closed Hotel", "status": "paused"})
    install(monkeypatch, fake)
    return fake


def _reconcile(monkeypatch, answers: dict):
    """Stand in for the recomputation, so the task's own logic is what is tested."""
    import sys
    from pathlib import Path

    scripts = Path(__file__).resolve().parent.parent / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import reconcile_inventory

    monkeypatch.setattr(reconcile_inventory, "disagreements",
                        lambda _db, pid: answers[pid])


CLEAN = {"oversold": 0, "stranded": 0, "wrong": [], "rooms": {}}


class TestItFindsDrift:
    def test_a_clean_estate_reports_nothing(self, db, monkeypatch, caplog):
        _reconcile(monkeypatch, {A: CLEAN, B: CLEAN})
        with caplog.at_level(logging.ERROR):
            out = messaging_tasks.reconcile_inventory_task()
        assert out == {"checked": 2, "drifting": 0, "properties": []}
        assert not [r for r in caplog.records if "inventory-drift" in r.getMessage()]

    def test_an_oversold_property_is_reported(self, db, monkeypatch, caplog):
        """
        The dangerous direction: live reservations the counter does not know
        about, so the hotel can sell those nights again.
        """
        _reconcile(monkeypatch, {
            A: {"oversold": 18, "stranded": -18, "wrong": [1, 2, 3], "rooms": {}},
            B: CLEAN})
        with caplog.at_level(logging.ERROR):
            out = messaging_tasks.reconcile_inventory_task()

        assert out["drifting"] == 1 and out["properties"] == [A]
        logged = [r.getMessage() for r in caplog.records if "inventory-drift" in r.getMessage()]
        assert len(logged) == 1
        assert "'oversold': 18" in logged[0]
        assert "Casa Mandala" in logged[0]

    def test_a_stranded_property_is_reported_too(self, db, monkeypatch, caplog):
        """
        The quiet direction: the counter says sold and nothing agrees, so the
        hotel has taken rooms off the market for nobody.
        """
        _reconcile(monkeypatch, {
            A: CLEAN, B: {"oversold": 0, "stranded": 5, "wrong": [1], "rooms": {}}})
        with caplog.at_level(logging.ERROR):
            out = messaging_tasks.reconcile_inventory_task()
        assert out["properties"] == [B]

    def test_it_names_the_nights(self, db, monkeypatch, caplog):
        _reconcile(monkeypatch, {
            A: {"oversold": 6, "stranded": 0, "wrong": [1, 2, 3], "rooms": {}},
            B: CLEAN})
        with caplog.at_level(logging.ERROR):
            messaging_tasks.reconcile_inventory_task()
        assert "'nights_affected': 3" in caplog.records[0].getMessage()


class TestItNeverCorrects:
    def test_it_writes_nothing(self, db, monkeypatch):
        """
        The load-bearing decision. A hold in flight and a booking being made in
        the same second both look like disagreement for a moment, and a task
        that corrected them would take a room off a guest who is paying.
        """
        _reconcile(monkeypatch, {
            A: {"oversold": 18, "stranded": -18, "wrong": [1], "rooms": {}},
            B: CLEAN})
        before = {t: [dict(r) for r in rows] for t, rows in db.store.items()}
        messaging_tasks.reconcile_inventory_task()
        after = {t: [dict(r) for r in rows] for t, rows in db.store.items()}
        assert before == after, "the reconciliation task mutated the database"

    def test_the_task_body_contains_no_write(self):
        import inspect

        import re

        source = inspect.getsource(messaging_tasks.reconcile_inventory_task)
        # Database writes only. A bare `.insert(` also matches `sys.path.insert`,
        # which is not a mutation and would make this assertion unmeetable.
        writes = re.findall(
            r"\b(?:db|get_db\(\))[^\n]*\.(update|insert|delete|upsert)\(", source)
        writes += re.findall(r"\bdb\.rpc\(", source)
        assert not writes, f"the reconciliation task writes: {writes}"


class TestItKeepsGoing:
    def test_one_unreadable_property_does_not_stop_the_sweep(self, db, monkeypatch,
                                                             caplog):
        """
        The whole point is the sweep across all of them. One property that
        cannot be read must not hide drift at the next.
        """
        def _one_explodes(_db, pid):
            if pid == A:
                raise RuntimeError("connection reset")
            return {"oversold": 4, "stranded": 0, "wrong": [1], "rooms": {}}

        import sys
        from pathlib import Path

        scripts = Path(__file__).resolve().parent.parent / "scripts"
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        import reconcile_inventory

        monkeypatch.setattr(reconcile_inventory, "disagreements", _one_explodes)
        with caplog.at_level(logging.WARNING):
            out = messaging_tasks.reconcile_inventory_task()
        assert out["properties"] == [B], "a failure at one hotel hid drift at another"

    def test_it_only_looks_at_hotels_that_are_open(self, db, monkeypatch):
        _reconcile(monkeypatch, {A: CLEAN, B: CLEAN})
        out = messaging_tasks.reconcile_inventory_task()
        assert out["checked"] == 2, "a paused hotel was reconciled"

    def test_a_broken_property_list_is_not_a_crash(self, db, monkeypatch):
        def _explode(*_a, **_k):
            raise RuntimeError("down")

        monkeypatch.setattr(db, "table", _explode)
        assert messaging_tasks.reconcile_inventory_task() == {"checked": 0,
                                                             "drifting": 0}


class TestItIsActuallyScheduled:
    """
    §16's lesson, applied here before it becomes an incident: the waiting-guest
    sweep sat in a schedule that no running beat owned, for four months.
    """

    def test_it_is_on_the_staysuite_beat(self, monkeypatch):
        monkeypatch.setenv("CELERY_BEAT_ROLE", "staysuite")
        import importlib

        from app.workers import celery_app as module

        importlib.reload(module)
        schedule = module._beat_schedule()
        assert "staysuite-inventory-drift" in schedule
        assert schedule["staysuite-inventory-drift"]["task"] == \
            "safar.pms.reconcile_inventory"

    def test_and_the_task_it_names_exists(self):
        from app.workers.celery_app import celery_app

        importlib_task = "safar.pms.reconcile_inventory"
        assert importlib_task in celery_app.tasks, (
            "beat would fire this name into the void")
