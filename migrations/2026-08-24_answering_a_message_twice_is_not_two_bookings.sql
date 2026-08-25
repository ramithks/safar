-- One guest message, one effect.
--
-- `messaging/pipeline` stamps `agent_run_id` on a message the moment the turn
-- returns, and that stamp is what stops the next drain answering it again. The
-- stamp is a database write, inside a Celery task declared `acks_late` with
-- `autoretry_for=(Exception,)`, so this ordering exists:
--
--     run_turn → create_booking_payment_link → a Razorpay order exists
--              → _mark_answered ✗ (reset connection, OOM, redeploy)
--              → Celery redelivers; the message is still pending
--              → the whole turn runs again → a second order for the same stay
--
-- The stamp cannot be made unfailable: swallowing its error would bring the
-- same message straight back from `_pending` on the next iteration of the drain
-- loop. So the mark stays fallible and the mutating tools underneath it are
-- made safe when it fails.
--
-- The key is sha256(message id · tool · validated arguments). Null for reads,
-- for staff turns and for the approval queue — a receptionist pressing a button
-- twice means it twice.

alter table public.agent_actions
    add column if not exists idempotency_key text;

comment on column public.agent_actions.idempotency_key is
    'sha256(inbound message id, tool, validated arguments) for a mutating call '
    'made on a guest turn. Null for reads and for anything a person initiated. '
    'Looked up by services/ai/registry.execute before the tool body runs.';

-- Partial, and on `executed` alone.
--
-- A refusal must stay reachable: the next attempt may run at a different
-- autonomy level or after a grant was fixed, and answering it from a stale
-- refusal would hide the fix until the guest wrote again. An `awaiting_approval`
-- row is not a result either — the work has not happened, and a replay should
-- queue behind the same human rather than be told it is done.
--
-- Not UNIQUE. The lookup is a read, the conversation lock already serialises
-- writers, and a unique index here would turn a lost race into a 500 on a
-- guest's turn instead of a duplicate row somebody can reconcile. It is an
-- index to make the lookup cheap, not a constraint to make it correct.
create index if not exists agent_actions_idempotency_idx
    on public.agent_actions (property_id, idempotency_key)
    where idempotency_key is not null and status = 'executed';

-- `replayed` has to be a status the table accepts.
--
-- Found by reading the live constraint rather than by a test: `agent_actions`
-- carries `agent_actions_status_check`, and nothing in the suite writes through
-- it because `tests/fake_supabase` does not enforce check constraints. Without
-- this the guard would still have worked in production and every replay would
-- have written *no audit row at all* — `runtime._write_action` catches its own
-- failure and logs, deliberately, so that a broken audit trail never takes a
-- guest's answer with it. The result would have been a silent hole in exactly
-- the records somebody investigating a duplicate booking would go looking for.
alter table public.agent_actions
    drop constraint if exists agent_actions_status_check;

alter table public.agent_actions
    add constraint agent_actions_status_check
    check (status = any (array[
        'proposed', 'awaiting_approval', 'approved', 'executed',
        -- Succeeded without running: this call was already made in service of
        -- this same guest message. Distinct from 'executed' so that "what did
        -- this run do" never reads as "it made a second booking".
        'replayed',
        'rejected', 'failed', 'refused'
    ]));

-- ORDERING: this migration must be applied BEFORE the code that writes the
-- column is deployed. `runtime._write_action` sends `idempotency_key` on every
-- action row, and PostgREST rejects an insert naming a column that does not
-- exist — so code-first would mean every action row failing, silently, because
-- that write catches its own exception on purpose.
