"""
The chain from a schedule entry to a manager's phone, proven link by link.

**Why this file exists at all.** Every link in this chain already had a test.
`test_waiting_guests.py` proves the sweep finds a waiting guest and notifies
once. `test_escalation_paths.py` proves every way a turn can end badly reaches
the state the sweep looks for. `test_celery_routing.py` proves the task routes
to the `hotelos` queue.

All of them passed, for months, while `agent_escalations` had **zero rows for
all time**. Nothing was scheduling the task.

The gap was between the links: `docker-compose.stayos.yml` had no beat service,
deliberately, justified by a comment saying every entry in `beat_schedule` was a
marketplace task — true when written, false the day the sweep was added. And
even if a beat had existed, the marketplace one enqueues onto the marketplace
broker while the StayOS worker listens to StayOS's own Redis, so the two would
never have met.

§9 is explicit about what that means for this file: *do not count "beat service
exists" as success — prove the task executes*. So the last test here does not
inspect configuration. It seeds a guest who has been waiting twenty minutes,
calls the Celery task body itself, and asserts a manager's phone was written to.

The four links, in order:

    1  the schedule contains the sweep, under the StayOS role
    2  the name in the schedule resolves to a task that is actually registered
    3  that task routes to the queue the StayOS worker consumes
    4  running it notifies a hotel about a real waiting guest

Link 2 is the one that looks like paranoia and is not: a schedule entry is a
*string*, and a string that names nothing produces a beat that ticks happily and
enqueues a task no worker can resolve.
"""
import asyncio
import os
import pathlib

import pytest

from app.services.pms import escalation, waiting
from app.workers import celery_app as celery_module
from tests.fake_supabase import FakeDb

PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

SWEEP = "travbuddy.messaging.sweep_waiting_guests"

COMPOSE = pathlib.Path(__file__).resolve().parent.parent / "docker-compose.stayos.yml"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def role(monkeypatch):
    """Set CELERY_BEAT_ROLE for one test without leaking it into the next."""
    def _set(value: str | None):
        if value is None:
            monkeypatch.delenv("CELERY_BEAT_ROLE", raising=False)
        else:
            monkeypatch.setenv("CELERY_BEAT_ROLE", value)
        return celery_module._beat_schedule()
    return _set


# ── link 1: the schedule ────────────────────────────────────────────────────

def test_a_stayos_beat_schedules_the_sweep(role):
    schedule = role("stayos")
    assert SWEEP in {entry["task"] for entry in schedule.values()}


def test_a_stayos_beat_schedules_nothing_that_writes_to_a_marketplace_table(role):
    """
    The reason the schedule is split by role rather than shared.

    `revert_intro_commission` writes hourly to `partners` — a table that exists
    in both databases and means something entirely different in each. A StayOS
    beat running it would rewrite hotel rows with marketplace semantics, which
    is the one outcome worse than the sweep not running at all.
    """
    tasks = {entry["task"] for entry in role("stayos").values()}
    assert tasks == {SWEEP}
    assert not any("partners" in task for task in tasks)


def test_the_marketplace_beat_is_unchanged_by_any_of_this(role):
    """
    §12. The marketplace deployment sets no role and must not need a redeploy;
    `all` is the default and still schedules everything.
    """
    everything = role(None)
    marketplace = role("marketplace")
    assert set(marketplace) <= set(everything)
    assert SWEEP in {e["task"] for e in everything.values()}
    assert SWEEP not in {e["task"] for e in marketplace.values()}


def test_an_unrecognised_role_schedules_everything_rather_than_nothing(role):
    """
    A typo in a compose file must not silently stop every scheduled task in the
    deployment. Loud in the log, harmless in effect.
    """
    schedule = role("stayoss")
    assert SWEEP in {e["task"] for e in schedule.values()}


# ── link 2: the name resolves ───────────────────────────────────────────────
#
# `include=[...]` on the app is honoured when a *worker* boots, not on import,
# so a test that merely imports `celery_app` sees an empty registry. Loading
# exactly that list here is the point rather than a workaround: it is what the
# worker does, and doing it the same way is what makes the assertion below mean
# something about production.

def _load_worker_task_modules() -> None:
    import importlib

    for module in celery_module.celery_app.conf.include or ():
        importlib.import_module(module)


_load_worker_task_modules()


@pytest.mark.parametrize("task_name", sorted(
    {e["task"] for e in celery_module._beat_schedule().values()}
))
def test_every_scheduled_name_is_a_task_that_exists(task_name):
    """
    A schedule entry is a string. A beat with a string that names nothing ticks
    every five minutes, enqueues a message no worker can resolve, and reports
    nothing wrong from either side.

    Run over *every* entry in the schedule rather than the sweep alone, because
    the failure is a class rather than an incident — and the marketplace
    entries are scheduled by the same beat in the default `all` role.
    """
    assert task_name in celery_module.celery_app.tasks, (
        f"{task_name!r} is scheduled and is not registered — the beat will "
        f"enqueue it and no worker will be able to run it"
    )


def test_the_include_list_covers_everything_the_schedule_names():
    """
    The two lists that have to agree and live two hundred lines apart.

    A task added to `beat_schedule` whose module is missing from `include` is
    the void the `include` comment warns about — the beat fires, the worker
    cannot resolve the name, and nothing anywhere says so. Derived from the
    registry after loading `include`, so it cannot be satisfied by editing one
    list alone.
    """
    scheduled = {e["task"] for e in celery_module._beat_schedule().values()}
    registered = set(celery_module.celery_app.tasks)
    missing = scheduled - registered
    assert not missing, (
        f"scheduled but not importable from celery_app.include: {sorted(missing)}"
    )


# ── link 3: the queue ───────────────────────────────────────────────────────

def test_the_sweep_routes_to_the_queue_the_stayos_worker_consumes():
    """
    Routing is by prefix — `travbuddy.messaging.*` → `hotelos` — which is what
    makes the task read the hotel database rather than the marketplace one.
    """
    routes = celery_module.celery_app.conf.task_routes
    resolved = None
    for pattern, target in routes.items():
        if pattern.rstrip("*") and SWEEP.startswith(pattern.rstrip("*")):
            resolved = target
    assert resolved and resolved.get("queue") == "hotelos", (
        f"{SWEEP} routes to {resolved!r}, not to the hotelos queue"
    )


def test_the_compose_file_runs_a_beat_and_says_which_one():
    """
    The configuration half, kept because the *absence* of this service is the
    original bug and a diff that removes it should fail something.

    Deliberately not the whole proof — the test below is. This asserts only
    that the deployment declares a beat, gives it the StayOS role, and points it
    at the same broker as the worker beside it. That last part is the one the
    marketplace beat got wrong.
    """
    text = COMPOSE.read_text()
    assert "CELERY_BEAT_ROLE=stayos" in text or "CELERY_BEAT_ROLE: stayos" in text
    assert "beat" in text
    # One broker for the pair. Two Redises is precisely how the sweep was
    # enqueued somewhere nothing was listening.
    assert text.count("redis://redis:6379") >= 2


# ── link 4: it actually runs ────────────────────────────────────────────────

@pytest.fixture
def wired(monkeypatch):
    """A hotel, a manager with a phone, and a guest who has been waiting."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    fake = FakeDb()
    for target in ("app.db.client", "app.services.pms.waiting",
                   "app.services.pms.escalation"):
        monkeypatch.setattr(f"{target}.get_db", lambda: fake)

    fake.seed("properties", {"id": PROPERTY, "partner_id": PARTNER,
                             "name": "Casa Mandala", "is_demo": False})
    fake.seed("hotel_team_members", {
        "id": "11111111-1111-4111-8111-111111111111", "property_id": PROPERTY,
        "role": "front_desk", "name": "Priya", "whatsapp": "919812345678",
        "is_active": True, "sort_order": 1,
    })
    fake.seed("conversations", {
        "id": "c0c0c0c0-0001-4001-8001-000000000001",
        "property_id": PROPERTY, "partner_id": PARTNER, "guest_id": None,
        "channel": "whatsapp",
        # The state all four failure paths converge on, and the one the sweep
        # exists to find: the AI promised a person and none came.
        "handler": "human_requested", "status": "open",
        "last_inbound_at": (now - timedelta(minutes=20)).isoformat(),
        "last_message_at": (now - timedelta(minutes=19)).isoformat(),
        "last_message": "Deluxe Balcony wala book kardo",
        "guest_profile": {}, "ai_enabled": True,
    })

    sent: list = []

    async def _notify(property_id, recipients, message, escalation_id):
        sent.append({"to": list(recipients), "message": message})
        return True

    monkeypatch.setattr(escalation, "_notify", _notify)
    return fake, sent


def test_running_the_scheduled_task_notifies_the_hotel(wired):
    """
    **The test §9 asked for.** Not "a beat service exists" — the task body
    itself, the one the beat enqueues by name, executed against a guest who has
    been waiting twenty minutes.

    Called through `celery_app.tasks[...]` rather than by importing the
    function, so what runs is what the beat's message would resolve to. A task
    that was renamed, unregistered or moved fails here.
    """
    fake, sent = wired

    task = celery_module.celery_app.tasks[SWEEP]
    result = task.run()

    assert result["waiting"] == 1, result
    assert result["raised"] == 1, result
    assert result["notified"] == 1, result

    assert sent, "the sweep raised an escalation and nobody was told"
    # The recipients are team-member rows, so the phone is read off the row
    # rather than assumed to be the row — the notifier needs the name for the
    # message and the number for the send.
    assert [m["whatsapp"] for m in sent[0]["to"]] == ["919812345678"]
    assert "Casa Mandala" in sent[0]["message"]

    rows = fake.rows("agent_escalations")
    assert len(rows) == 1
    assert rows[0]["kind"] == "unanswered"
    assert rows[0]["status"] in ("open", "notified")


def test_a_second_tick_five_minutes_later_does_not_notify_again(wired):
    """
    The sweep runs every five minutes and the guest stays waiting. A manager
    whose phone buzzes twelve times an hour about one guest has learned to
    ignore it, which is the same product as no sweep.
    """
    fake, sent = wired
    task = celery_module.celery_app.tasks[SWEEP]

    task.run()
    second = task.run()

    assert second["waiting"] == 1, "the guest is still waiting and must still be seen"
    assert second["notified"] == 0
    assert len(sent) == 1
    assert len(fake.rows("agent_escalations")) == 1


def test_a_demo_hotel_never_reaches_a_real_phone(wired):
    """
    §13. Casa Mandala has a real WhatsApp number on its team and every e2e suite
    in the repository parks conversations on it. Nine were sitting in
    `human_requested` the day the sweep was written.
    """
    fake, sent = wired
    fake.table("properties").update({"is_demo": True}).eq("id", PROPERTY).execute()

    result = celery_module.celery_app.tasks[SWEEP].run()

    assert result["waiting"] == 1
    assert result["notified"] == 0
    assert sent == []


def test_the_task_never_raises_out_of_the_beat(wired, monkeypatch):
    """
    A scheduled task that raises is a red row in a dashboard nobody watches.
    The next sweep is five minutes away, so a failure is logged and swallowed —
    and this asserts the swallow is honest about having happened.
    """
    async def _explode(**kwargs):
        raise RuntimeError("supabase is down")

    monkeypatch.setattr(waiting.escalation_service, "raise_escalation", _explode)

    result = celery_module.celery_app.tasks[SWEEP].run()
    assert result["notified"] == 0
