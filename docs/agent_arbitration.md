# The Action Arbiter

*Who decides that a conversation stops, and how that stopped being the model's
decision alone.*

---

## 1. The failure

A guest asks for a better price. The model calls

```
request_human_handoff(reason="guest_asked")
```

and the turn ends. Nobody asked for a person. The pricing engine could have
answered in four seconds. The guest is told somebody will get back to them, and
at eleven at night nobody does.

Handoff was the one decision in the system with no second opinion. The runtime
set `handoff = True`, and every deterministic layer downstream stood down —
including the one whose entire job is making sure a hotel's revenue-generating
turns actually run their tool, whose gate reads, literally, `not handoff`.

That held while the model in production reached for the tool rarely. It stopped
holding the moment a model reached for it more often.

## 1a. What the measurement said, once the measurement was fixed

The failure above is real and reproducible **with a scripted model**. It is not
what the original numbers were measuring.

`scripts/agent_eval.py` created its conversations without `last_inbound_at`.
`messaging.window_open` is false when that column is null, so `send_message`
refused with `MESSAGE_WINDOW_CLOSED` on every turn — and the agent's documented
answer to a refused action is to hand off. A real guest turn cannot be in that
state: the turn is *triggered by* an inbound message and the pipeline stamps
the column before the agent runs. Only a synthetic conversation can be, and
every conversation the evaluator made was.

It was invisible while the model in production answered in prose. The moment a
model started answering through `send_message` instead — a legitimate strategy
the tool exists for, and the one its own description prescribes — that model hit
a refusal on every turn, handed off on every turn, and the report attributed it
to negotiation.

With the column set and everything else held still, 30 runs per model:

| | Gemini 2.5 | Gemini 3.x |
|---|---|---|
| negotiation completed | 83.3% | **96.7%** |
| handoff rate | 20.0% | **3.3%** |
| latency p50 | 70.5s | **11.7s** |

Not one of the seven remaining handoffs was a `request_human_handoff` call.
Every one is the provider-failure path.

**So this layer is not a fix for an observed live failure rate.** It is the
removal of a dependency: the system no longer relies on the model not having
that bug. Which is the right thing to build either way — but the difference
between those two claims matters, and the numbers say which one is true.

## 2. What the fix is not

**It is not "put the old model back."** That leaves the system in the state
where the *next* model can end a sale by asserting something untrue, which is
the state worth getting out of.

**It is not "stop the model handing off."** Stopping is genuinely better than
guessing — that is the doctrine the whole agent is built on — and a layer that
could veto a stop outright would be a worse system than the one with the bug.

**It is not a prompt.** Whether the guest asked for a person is decidable by
looking at what the guest wrote. A deterministic problem does not get solved
with a prompt.

## 3. What it is

A *claim* is not a *fact*.

`reason="guest_asked"` is the model asserting something about the conversation.
The conversation is right there. So the reason is mapped to a business category
and the checkable categories are checked against evidence the turn already
holds — using, for "did the guest ask for a person", the very same detector that
*forces* a handoff when it matches. One detector, two directions, so the two can
never disagree.

```
Guest
  ↓
Intent / Context            required_actions.detect — regex + structured session
  ↓
Required Action             what this turn owes, before the model sees it
  ↓
LLM proposes                a tool call, or a handoff
  ↓
ACTION ARBITER              arbiter.judge — is the claim supported?
  ↓
  ├─ valid    → registry.execute → handoff happens
  └─ rejected → refusal + instruction → model calls the business tool
  ↓
Business Engine             price / inventory / booking / payment
  ↓
LLM response                the model writes the sentence, in the guest's language
```

The arbiter sits at `app/services/ai/arbiter.py` and runs as a **precheck** on
`request_human_handoff`. That placement is most of the design: `registry.execute`
runs prechecks before the tool body and before the autonomy gate, so a
turned-down handoff never moves `conversations.handler` and there is nothing to
compensate for.

## 4. The four properties

Listed in the order they matter, because each one is a way this layer could be
worse than the bug it fixes.

### It can only ever delay a stop, never prevent one

`TurnState.handoff_rejected` makes the *second* request in a turn unconditional.
The arbiter's power is to make the model try one more thing, and it spends that
power once. Worst case: one extra tool call before the conversation reaches a
person. Never: a guest who asked for a human and did not get one.

### It never overrides a safety stop

| category | source reasons | checked? |
|---|---|---|
| `SAFETY_ESCALATION` | `complaint` | never |
| `POLICY_REQUIRES_HUMAN` | `refund`, `policy_conflict` | never |
| `AGENT_FAILURE` | a tool that actually broke | never |
| `STAFF_REQUESTED` | — | never |
| `GUEST_REQUESTED_HUMAN` | `guest_asked` | against the guest's words |
| `AUTONOMY_REQUIRES_APPROVAL` | `autonomy_limit` | against what queued this turn |
| `UNSUPPORTED_REQUEST` | `no_knowledge`, `low_confidence`, `unusual_request` | against what is outstanding |

`POLICY_REQUIRES_HUMAN` is deliberately not checkable. Whether a refund
conversation goes to a person is the hotel's decision, and overriding it would
be this layer changing a customer's configuration to improve its own numbers.

**And the guest's own words outrank the model's label for them.** A guest who
writes "manager se baat karao" gets a person even if the model files the handoff
as `no_knowledge`. The reason string is the model's *description* of the
conversation; the conversation is the conversation, and when they disagree about
something readable, the conversation wins — in the direction of stopping, which
is the only direction it is safe to be wrong in.

### It does not bypass autonomy

Nothing in `arbiter.py` executes anything. There is no `await` in it, no
database call, and a test parses its AST to keep it that way. Turning a handoff
down puts the model back in front of `registry.execute`, which runs permission,
grant, argument validation, the property's own rules and `autonomy.decide`
exactly as it always did.

An agent at level 2 sent back to negotiate gets `awaiting_approval` — the
correct business outcome, honest to the guest — and *that* corroborates the
`autonomy_limit` handoff the model then asks for, which goes through. The loop
closes rather than fighting itself. The ladder answers; the arbiter never
guesses what it would have said.

### It requires somewhere better to go

An uncorroborated reason is **not** by itself grounds to refuse. The model may
be right in a way nothing here can see. A handoff is turned down only when the
system positively knows a business action it could take instead — unmet,
granted to this agent, and not already tried this turn.

"I do not know" is only contradicted when the system does know.

## 5. The second cause

The arbiter fixes turns where the deterministic layer has an opinion. It found
one where the layer had none.

`NEGOTIATION` went **advisory** whenever the session had no rate plan — which is
the ordinary first turn. "Best price kya de sakte ho?" arrives before anything
has been priced far more often than after. So on the turn where the model was
most likely to form a bad opinion, nothing had a better one.

It was also naming the wrong tool: `negotiate_rate`, on a session with nothing
to discount.

Now the requirement is real and what it requires is **discovery**. A hotelier
asked "what's your best price" says "for which nights?" — that is the required
action, and `get_stay_options` is the tool that is it.

| session state | requirement | forced by the application? |
|---|---|---|
| rate plan + dates | `negotiate_rate` | yes — arguments are constructible |
| dates, no rate plan | `get_stay_options` | yes — the lookup is fully specified |
| neither | `get_stay_options` | **no** — there is nothing honest to invent |

`must_execute` means *this turn owes the guest something*. `forceable` means
*the application will run it itself*. Separating them is what lets a requirement
be real without being executable.

## 6. Observability

Every handoff the model asks for lands on `agent_runs.required_actions.handoff_judgements`
— accepted and rejected alike, with the category, the reason given, the tool
that was outstanding, and one line of why. A record that kept only the
rejections would make the layer look busier than it is.

`AGENT_ARBITRATE_HANDOFFS=false` turns the enforcement off without a deploy.
**The judgement is still recorded**, so a property running with it off can still
be asked how often it *would* have fired — which is what makes the before/after
measurable on the same day against the same hotel, and a rollback measurable
rather than blind.

## 7. Where it is tested

| file | what it holds |
|---|---|
| `tests/test_arbiter.py` | what the arbiter decides, and that it decides without acting |
| `tests/test_handoff_arbitration.py` | the whole turn, with the model's failing call scripted |
| `tests/test_required_actions.py` | what a message requires |
| `tests/test_forced_actions.py` | what the runtime does about an unmet requirement |
| `scripts/handoff_forensics.py` | the live capture: same turn, both models, everything recorded |
