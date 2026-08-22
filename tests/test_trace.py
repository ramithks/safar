"""
"Why didn't this booking confirm?" has to be answerable from logs.

One guest booking crosses a Meta webhook, a Celery worker, a model call, a tool
and a Razorpay webhook. Until now the only thing tying those lines together was
the clock, so reconstructing one booking meant grepping a timestamp range and
hoping no other hotel was busy.

Two properties matter and both are easy to get subtly wrong:

**It has to survive the boundaries this codebase actually crosses.** `await`, a
thread pool (every database call goes through `run_db`), and a Celery task in
another process. A trace that quietly resets at any of those is worse than none,
because it looks like it is working.

**It has to be restored afterwards.** One worker handles many conversations. A
leaked `conversation_id` labels the next hotel's lines with the last one's, and
somebody debugging then reads a booking that never happened.
"""
import asyncio
import logging

import pytest

from app.services import trace


@pytest.fixture(autouse=True)
def clean():
    """Each test starts from nothing, whatever the last one bound."""
    token = trace._context.set({})
    yield
    trace._context.reset(token)


# ── carrying ────────────────────────────────────────────────────────────────

def test_a_span_mints_an_id_when_it_is_given_none():
    with trace.span() as request_id:
        assert request_id
        assert trace.current()["request_id"] == request_id


def test_a_span_honours_an_id_it_is_handed():
    """
    What lets the Celery worker's lines join the webhook's rather than starting
    a second trace for the same guest message.
    """
    with trace.span(request_id="abc123") as request_id:
        assert request_id == "abc123"


def test_binding_is_additive_across_stages():
    """
    The webhook knows the conversation, the pipeline learns the hotel, the tool
    learns the booking. No stage has to know what the next one will need.
    """
    with trace.span(conversation_id="conv-1"):
        trace.bind(hotel_id="hotel-1")
        trace.bind(booking_id="book-1")

        now = trace.current()
        assert now["conversation_id"] == "conv-1"
        assert now["hotel_id"] == "hotel-1"
        assert now["booking_id"] == "book-1"


def test_the_context_is_restored_when_a_span_ends():
    """
    The property that keeps one worker's conversations apart. Without it the
    next hotel's lines carry the last one's ids.
    """
    with trace.span(conversation_id="first"):
        assert trace.current()["conversation_id"] == "first"
    assert "conversation_id" not in trace.current()

    with trace.span(conversation_id="second"):
        assert trace.current()["conversation_id"] == "second"


def test_it_survives_await():
    async def inner():
        await asyncio.sleep(0)
        return trace.current().get("conversation_id")

    async def outer():
        with trace.span(conversation_id="conv-1"):
            return await inner()

    assert asyncio.run(outer()) == "conv-1"


def test_it_survives_the_thread_pool_every_database_call_uses():
    """
    `run_db` hands work to an executor. `contextvars` is copied into
    `asyncio.to_thread`, and if it were not, every line a query logged would be
    unattributed — which is most of the interesting ones.
    """
    async def outer():
        with trace.span(conversation_id="conv-1", hotel_id="hotel-1"):
            return await asyncio.to_thread(lambda: trace.describe())

    described = asyncio.run(outer())
    assert "conv=conv-1" in described
    assert "hotel=hotel-1" in described


# ── the log line ────────────────────────────────────────────────────────────

def test_the_prefix_reads_the_way_somebody_greps_for_it():
    with trace.span(request_id="r1", conversation_id="c1"):
        trace.bind(hotel_id="h1", booking_id="b1")
        assert trace.describe() == "[req=r1 conv=c1 hotel=h1 booking=b1] "


def test_an_empty_trace_produces_no_prefix_at_all():
    """A startup line should not be `[] message`."""
    assert trace.describe() == ""


def test_the_filter_puts_the_trace_on_every_record(caplog):
    """
    A `Filter` on the root rather than a `Formatter`, so library records get it
    too — the httpx line saying a call timed out is exactly the one you want the
    conversation id on, and nobody is going to edit httpx.
    """
    log = logging.getLogger("test.trace")
    log.addFilter(trace.TraceFilter())

    with caplog.at_level(logging.INFO):
        with trace.span(conversation_id="conv-9"):
            log.info("something happened")

    assert "conv=conv-9" in caplog.records[-1].trace


def test_a_record_emitted_outside_any_span_still_formats():
    """
    `%(trace)s` is in the format string, so a record without the attribute would
    raise inside the handler — which takes down the thing it was watching.
    """
    log = logging.getLogger("test.trace.bare")
    log.addFilter(trace.TraceFilter())
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", None, None)
    trace.TraceFilter().filter(record)
    assert logging.Formatter(trace.LOG_FORMAT).format(record).endswith("hello")


# ── what must never be in it ────────────────────────────────────────────────

def test_only_the_named_fields_are_carried():
    """
    A closed list, not an open dict. An open one invites somebody to put a phone
    number in it, and a log aggregator then keeps it for a year — which is
    exactly what §29 forbids.
    """
    with trace.span():
        trace.bind(guest_phone="+919876543210", guest_name="Sahil",
                   card="4111111111111111", authorization="Bearer abc")

        described = trace.describe()
        assert "9876543210" not in described
        assert "Sahil" not in described
        assert "4111" not in described
        assert "Bearer" not in described


def test_an_unknown_field_is_dropped_rather_than_raising():
    """
    This runs on the path of a guest's message. A typo in a log field must never
    be the thing that stops a hotel replying.
    """
    with trace.span():
        trace.bind(converstaion_id="typo")   # sic
        assert "typo" not in trace.describe()


def test_a_broken_filter_never_takes_down_the_thing_it_is_watching(monkeypatch):
    monkeypatch.setattr(trace, "describe", lambda: 1 / 0)
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", None, None)

    assert trace.TraceFilter().filter(record) is True
    assert record.trace == ""


def test_an_empty_value_does_not_create_an_empty_field():
    """
    `guest_id` is null on every conversation until a booking links one. That
    should read as absent, not as `guest=None`.
    """
    with trace.span(conversation_id="c1"):
        trace.bind(guest_id=None, booking_id="")
        assert trace.describe() == "[req=" + trace.current()["request_id"] + " conv=c1] "
