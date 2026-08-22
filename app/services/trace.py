"""
Answering "why didn't this booking confirm?" without reading server logs by hand.

A single guest booking crosses five processes' worth of code — a Meta webhook, a
Celery worker, a model call, a tool, a Razorpay webhook — and until now the only
thing tying those lines together was the clock. Reconstructing one booking meant
grepping a timestamp range and hoping no other hotel was busy.

**One id, carried, and stamped on every line.** The webhook mints it, the Celery
task inherits it, and every log record emitted underneath carries it along with
whichever of `conversation_id`, `guest_id`, `hotel_id` and `booking_id` are known
at that point. Then the question above is one grep.

    2026-08-23 19:58:52 pms INFO [req=8f2a1c4b conv=e58d75c2 hotel=harry
                                  booking=6585c7ea] payment settled, fulfilling

**A ContextVar rather than a parameter.** Threading a request id through forty
functions is a refactor nobody finishes, and the half that gets done is the half
that was already easy to debug. `contextvars` follows `await` and is copied into
`asyncio.to_thread` and `run_in_executor`, which is where this codebase's
database calls run.

**What is never in it.** No token, no signature, no card, no phone number, no
guest name. The ids here are ours and mean nothing outside our own database —
which is the property that makes it safe to turn the log level up in production
when something is going wrong.
"""
import logging
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

# The fields worth carrying. Deliberately a closed list: an open dict invites
# somebody to put a phone number in it, which is exactly what §29 forbids and
# what a log aggregator then keeps for a year.
_FIELDS = ("request_id", "conversation_id", "guest_id", "hotel_id",
           "booking_id", "run_id", "order_id")

_context: ContextVar[dict] = ContextVar("trace_context", default={})

# What each field is called in a log line. Short, because these prefix every
# record and a prefix longer than the message is a prefix people filter out.
_LABELS = {
    "request_id": "req", "conversation_id": "conv", "guest_id": "guest",
    "hotel_id": "hotel", "booking_id": "booking", "run_id": "run",
    "order_id": "order",
}


def new_id() -> str:
    """A fresh correlation id. Short enough to paste into a chat message."""
    return uuid.uuid4().hex[:12]


def current() -> dict:
    return dict(_context.get())


def bind(**fields) -> None:
    """
    Add what we have just learned to the current trace.

    Additive on purpose. The webhook knows the conversation; the pipeline learns
    the hotel; the tool learns the booking. Each stage says what it found and
    nothing has to know what the next stage will need.

    Unknown keys are dropped rather than raising — this runs on the path of a
    guest's message, and a typo in a log field must never be the thing that
    stops a hotel replying.
    """
    context = dict(_context.get())
    for key, value in fields.items():
        if key in _FIELDS and value:
            context[key] = str(value)
    _context.set(context)


@contextmanager
def span(**fields):
    """
    A trace for one unit of work, restored afterwards.

    Used at the two entry points that start something: the HTTP middleware and
    the Celery task. Restoring on exit matters in the worker, where one process
    handles many conversations and a leaked `conversation_id` would label the
    next hotel's logs with the last one's.
    """
    token = _context.set({**_context.get(),
                          "request_id": fields.pop("request_id", None) or new_id(),
                          **{k: str(v) for k, v in fields.items()
                             if k in _FIELDS and v}})
    try:
        yield _context.get()["request_id"]
    finally:
        _context.reset(token)


def describe() -> str:
    """The current trace as a log prefix, or empty when there is nothing to say."""
    context = _context.get()
    if not context:
        return ""
    parts = [f"{_LABELS[k]}={context[k]}" for k in _FIELDS if context.get(k)]
    return f"[{' '.join(parts)}] " if parts else ""


class TraceFilter(logging.Filter):
    """
    Puts the trace on every record, without every call site asking for it.

    A `Filter` rather than a `Formatter` because a filter attaches to the *root*
    and therefore covers libraries too — the httpx line that says a call timed
    out is exactly the line you want the conversation id on, and nobody is going
    to go and edit httpx.

    Never raises. A logging handler that throws takes down the thing it was
    watching, and this one runs on every record in the process.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.trace = describe()
        except Exception:  # noqa: BLE001
            record.trace = ""
        return True


LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(trace)s%(message)s"


def install(root: logging.Logger | None = None) -> None:
    """
    Attach the filter and the format to everything already configured.

    Called after `logging.basicConfig`, on the handlers it made, rather than
    replacing them: `basicConfig` is a no-op the second time and a process that
    reconfigures logging halfway through is one where the first hundred lines
    have a different shape from the rest.
    """
    log = root or logging.getLogger()
    trace = TraceFilter()
    log.addFilter(trace)
    for handler in log.handlers:
        handler.addFilter(trace)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
