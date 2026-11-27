# NOTICE

Safar is built on top of the TravBuddy Backend, which Sahil Verma open-sourced
in November 2026 after shutting the TravBuddy product down.

- Upstream source: https://github.com/sahil28123/travbuddy_backend
- The upstream repository carries no LICENSE file. Its README states the code
  is published "so you can self-host, run, or build on top of any part of the
  stack", and the author's announcement invited builders to use it. Safar is
  such a build-on-top reuse: the name and brand have been changed, the Google
  Vertex AI / Gemini dependency has been replaced with free open models, and
  the deployment runs entirely on free tiers.
- All credit for the original architecture and engineering goes to the
  TravBuddy author. If you are the author and want this reuse changed or
  removed, open an issue and it will be addressed.

Safar changes vs upstream (summary):

1. LLM backend: new `app/services/freellm.py` transport — every LLM call (trip
   planner, journey reasoning, extraction, agent runtime, reel vision pipeline)
   can run on free OpenAI-compatible models instead of Vertex AI Gemini
   (`LLM_BACKEND=openrouter`). The Vertex path is untouched and still available.
2. Rebrand: TravBuddy / HostOS / StayOS names, domains and email identities
   replaced with Safar / StaySuite placeholders.
3. Optional integrations: Sentry disabled by default; Google Places, GCS,
   Razorpay, WhatsApp/Instagram messaging remain optional env-driven
   integrations that are simply left unconfigured in the free deployment.
4. UI: a branded single-page UI (static/) served by the API.
