"""
Messaging.

    Meta ──► webhook adapter ──► messaging service ──► conversation + message
                                        │
                                        ▼
                                  agent runtime
                                        │
                                        ▼
                          send_message ──► messaging service
                                        │
                                        ▼
                              channel adapter ──► Meta

The shape of that diagram is the point: **the runtime never touches a provider,
and the provider never touches the runtime.** The agent asks the messaging
service to send something; the messaging service decides whether it may, picks
the channel, and hands it to an adapter. Instagram arrives later as another
adapter behind the same call, not as another branch inside the agent.

Three rules live in this package rather than in a prompt, because each of them
is a rule a language model would be talked out of:

**The 24-hour window.** WhatsApp permits free-form replies only inside a window
that opens on each inbound message. `service.send()` refuses outside it and
returns `MESSAGE_WINDOW_CLOSED`, which the agent sees as a structured refusal it
can act on — pick an approved template, or hand off. A model asked to "check
whether the window is open" would sometimes decide it was.

**Idempotency, in both directions.** Meta retries webhooks and Meta times out on
sends. Inbound is keyed on `(channel, external_id)`; outbound on a
`client_reference` chosen before the request leaves. Without the first, one
guest message becomes two tickets and two replies. Without the second, a
timeout becomes a second message to a guest.

**Property identity comes from the connection, never from the payload.** An
inbound webhook says which phone number id it arrived on;
`whatsapp_connections` is the only thing that turns that into a property. A
`property_id` in a request body is a property_id the sender chose.

    channels.py   what a channel is, and what every adapter must do
    service.py    conversations, messages, the window, idempotency
    templates.py  what may be said when the window has closed
    whatsapp/     the Meta Cloud API adapter, its fake, and webhook parsing
"""
