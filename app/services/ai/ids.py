"""
An id a model made up is not an id.

Tool arguments that name a record were plain `str`, so a value the model
invented passed pydantic, passed the permission check, passed the autonomy
gate, and died in PostgREST:

    invalid input syntax for type uuid: "breakfast_included"

That is a real production transcript. A guest asked to book, the model guessed
a rate plan id from the *name* of the rate plan, and the tool call failed as an
unhandled error — so the guest was told "there is some trouble generating a
quote" and the conversation stopped there. The same shape reached
`quote_payment` a month earlier with a booking reference in a quote id field.

Validating here turns all of it into a refusal the model can act on, because
`registry.execute` catches `ValidationError` before anything runs and hands the
reason back as text. The model reads "that is not an id, call get_stay_options"
and does, instead of the hotel losing a booking to a 500.

**Deliberately format-only.** Whether the row exists, and whether this
conversation may see it, are different questions with different answers —
`ctx.require_reservation` and every tool's own scoping own those. This is the
cheap check that stops a guess reaching the database at all.
"""
import uuid
from typing import Annotated

from pydantic import AfterValidator


def _looks_like_an_id(value: str) -> str:
    text = (value or "").strip()
    try:
        uuid.UUID(text)
    except (ValueError, AttributeError, TypeError):
        raise ValueError(
            f"{text[:40]!r} is not an id. Ids come from a tool result — never "
            "from a name, a room type, a rate plan's label, or a booking "
            "reference. Call the tool that lists them and use the id it "
            "returns."
        ) from None
    return text


# Use for any argument naming a record: a room type, a rate plan, a quote, a
# hold, a reservation, a task, a service, a campaign.
ResourceId = Annotated[str, AfterValidator(_looks_like_an_id)]
