"""
WhatsApp Cloud API.

    adapter.py   sending, over httpx
    fake.py      a deterministic stand-in that implements the same protocol
    webhook.py   signature verification and payload parsing

`fake.py` is not a mock in the pejorative sense — it is a second implementation
of `ChannelAdapter`, selected by a column on the connection row
(`is_sandbox`), and it records what it was asked to send so a test can assert on
it. What it emphatically does **not** do is prove the Meta integration works.
Those are three different claims and this codebase keeps them apart:

    REAL DATABASE TESTED   the rows, the RLS, the constraints — yes, everywhere
    FAKE META TESTED       the adapter contract, the window, idempotency — yes
    REAL META TESTED       a message that actually reached a phone — not yet

A connection wired to the fake carries `is_sandbox = true` in Postgres, so
nothing has to infer it from a hostname and nothing can ship believing it is
talking to Meta when it is not.
"""
