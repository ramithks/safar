"""
Which deployment's worker may run which task.

This codebase runs twice — once against the marketplace Supabase project, once
against the hotel OS's — and one process has one `SUPABASE_URL`. A task
executed by the wrong deployment reads the wrong database.

Celery decides that, not the code that calls `.delay()`. Without routing every
task lands on the single default queue both workers consume, and whichever
worker grabs it first runs it. Sharing one Redis makes the collision certain
rather than likely, and the StayOS service is meant to be configured by copying
the marketplace's environment — `REDIS_URL` included.

The asymmetry in these tests is deliberate and is the whole design: the
marketplace worker is **already running in production with no `-Q`**, so its
tasks must keep landing on `celery`. Moving them to a symmetrical
`marketplace` queue would read better in a diagram and would stop reels, reco
and partner commissions dead on the next deploy.
"""

import pytest

from app.workers.celery_app import celery_app

HOTEL_OS_TASKS = [
    "travbuddy.messaging.process_conversation",
    "travbuddy.marketing.run_campaign",
    # Scheduled by the marketplace's beat — the only one that runs a beat — and
    # executed only by the StayOS worker. It reads `conversations`, which the
    # marketplace project has never had.
    "travbuddy.messaging.sweep_waiting_guests",
]

MARKETPLACE_TASKS = [
    "travbuddy.analyze_reel",
    "travbuddy.partners.revert_intro_commission",
    "travbuddy.reco.refresh_expected_price",
    "travbuddy.reco.refresh_destination_priors",
    "travbuddy.reco.fit_centroids",
    "travbuddy.reco.embed_and_profile",
    "travbuddy.reco.embed_hotel",
    "travbuddy.reco.profile_hotel",
]


def queue_for(task_name: str) -> str:
    route = celery_app.amqp.router.route({}, task_name)
    queue = route.get("queue")
    return getattr(queue, "name", queue) or celery_app.conf.task_default_queue


@pytest.mark.parametrize("task", HOTEL_OS_TASKS)
def test_hotel_os_tasks_go_to_the_hotelos_queue(task):
    """
    The direction that hurts. A guest's WhatsApp reply run by the marketplace
    worker looks for `conversations` in a project that has never had that
    table, and fails in a service nobody is watching.
    """
    assert queue_for(task) == "hotelos"


@pytest.mark.parametrize("task", MARKETPLACE_TASKS)
def test_marketplace_tasks_stay_on_the_default_queue(task):
    """
    Not `marketplace`. The production worker consumes `celery` today and its
    start command is not part of this change.
    """
    assert queue_for(task) == "celery"


def test_the_two_sets_never_share_a_queue():
    assert not (
        {queue_for(t) for t in HOTEL_OS_TASKS}
        & {queue_for(t) for t in MARKETPLACE_TASKS}
    )


def test_everything_beat_schedules_is_routed_to_a_deployment_that_can_run_it():
    """
    Beat runs in the marketplace deployment, and *enqueueing* is not
    *executing*. `revert_intro_commission` is unrouted, so beat's own worker
    picks it up — correct, because it writes to `partners`, a table that also
    exists meaning something entirely different in the hotel OS. The waiting
    sweep is routed to `hotelos`, so beat publishes it and the StayOS worker —
    the one process with StayOS's `SUPABASE_URL` — is the only one that can
    take it.

    The rule this holds is therefore not "no hotel-OS task in the schedule" but
    the thing that rule was standing in for: **nothing may be scheduled that
    the worker consuming its queue cannot run.** An entry added here with no
    route is one the marketplace worker will execute against the marketplace
    database, whatever it was written for.

    One operational consequence, recorded in `DEPLOYING_STAYOS.md`: the StayOS
    sweep stops if the *marketplace* beat stops. It is a scheduled reminder
    rather than part of any guest's path, so that degrades to no reminders
    rather than to anything a guest sees.
    """
    for name, entry in celery_app.conf.beat_schedule.items():
        task = entry["task"]
        if task in HOTEL_OS_TASKS:
            assert queue_for(task) == "hotelos", (
                f"{name} would be run by the marketplace worker against the "
                "marketplace database"
            )
        else:
            assert task in MARKETPLACE_TASKS, f"{name} is scheduled and unclassified"


def test_every_registered_task_is_routed_somewhere_a_worker_listens():
    """
    A task routed to a queue nobody consumes never runs, and nothing errors.
    The two queues in production are `celery` (marketplace worker, no -Q) and
    `hotelos` (StayOS worker, -Q hotelos).
    """
    listened = {"celery", "hotelos"}
    for task in HOTEL_OS_TASKS + MARKETPLACE_TASKS:
        assert queue_for(task) in listened, f"{task} lands on a queue nobody reads"
