"""
Instagram Messaging.

    adapter.py   sending, over httpx, to Meta's Instagram Messaging API
    fake.py      a deterministic stand-in implementing the same protocol
    webhook.py   signature verification and payload parsing

Instagram is a first-class channel here, not WhatsApp with a different logo.
Three differences are structural rather than cosmetic, and each is expressed as
code rather than as a comment:

  · **no templates** — nothing reopens a closed window, so `send_template`
    refuses in both the real adapter and the fake
  · **a longer human window** — Meta's human-agent allowance gives a *person*
    seven days against the agent's 24 hours, keyed on `conversations.handler`
    in `messaging/policy.py`
  · **no conversation billing** — Meta does not charge per Instagram
    conversation, so there are no `provider_cost_rates` rows for it and the
    cost side honestly reports "no cost rate matched"

The claims this package supports, kept apart on purpose:

    REAL DATABASE TESTED     the rows, the RLS, the constraints — yes
    FAKE INSTAGRAM TESTED    the adapter contract, windows, idempotency — yes
    REAL INSTAGRAM TESTED    a message that actually reached a phone — not yet
"""
