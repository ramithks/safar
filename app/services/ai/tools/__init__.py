"""
The tools themselves.

One rule holds across every module here, and it is the rule the whole product
depends on:

    **A tool contains no business logic.**

`search_availability` does not count rooms; it calls the same
`services/pms/availability.stay_options()` the front desk calls, which calls the
same `property_availability()` in Postgres. `get_quote` does not compute a
price; it calls `services/pms/booking.create_quote()`, which is where the GST
slab and the discount floor live. `create_task` does not insert a row; it calls
`services/pms/frontdesk.create_task()`, which numbers the ticket and writes its
event.

If a tool ever needs logic of its own, that is a signal the PMS is missing a
service — not a signal to write it here. A second implementation of availability
is a second answer to "can I sell this room", and the whole architecture exists
to have one.

The other rule, enforced in `registry.execute()` rather than here: no tool body
checks a permission. By the time a function in this package runs, the grant, the
scope, the autonomy level and the arguments have all been verified.
"""

# ── what the PMS calls an agent ──────────────────────────────────────────────
#
# Three tables, two words, because they were written months apart:
# `quotes.issued_by` and `reservations.created_by` enumerate 'ai';
# `tasks.created_by` enumerates 'agent'. Each check constraint is authoritative
# for its own table, so the tools use whichever word their table knows.
#
# Named here rather than spelled out at four call sites, so the inconsistency is
# visible in one place and can be normalised by a migration when something else
# is already touching those constraints — not by a migration whose only purpose
# is tidiness on a table that holds money.

ACTOR_QUOTE = "ai"          # quotes.issued_by
ACTOR_RESERVATION = "ai"    # reservations.created_by
ACTOR_TASK = "agent"        # tasks.created_by
