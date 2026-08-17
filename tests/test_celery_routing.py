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


def test_beat_only_schedules_marketplace_tasks():
    """
    Beat belongs to the marketplace deployment. `revert_intro_commission` runs
    hourly and writes to `partners` — a table that also exists, meaning
    something entirely different, in the hotel OS. If a hotel-OS task ever
    appears here, this schedule has to move behind a flag first.
    """
    scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    assert scheduled <= set(MARKETPLACE_TASKS)
    assert not scheduled & set(HOTEL_OS_TASKS)


def test_every_registered_task_is_routed_somewhere_a_worker_listens():
    """
    A task routed to a queue nobody consumes never runs, and nothing errors.
    The two queues in production are `celery` (marketplace worker, no -Q) and
    `hotelos` (StayOS worker, -Q hotelos).
    """
    listened = {"celery", "hotelos"}
    for task in HOTEL_OS_TASKS + MARKETPLACE_TASKS:
        assert queue_for(task) in listened, f"{task} lands on a queue nobody reads"
