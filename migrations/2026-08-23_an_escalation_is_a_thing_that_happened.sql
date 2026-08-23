-- An escalation is a thing that happened.
--
-- `agent_actions.result_type` enumerates what a tool can have produced — a
-- task, a quote, a hold, a reservation, a message. `escalate_to_team` produces
-- none of those: it produces a row in `agent_escalations` with a lifecycle of
-- its own, and the whole value of `result_id` is that "the AI said it asked the
-- hotel" and "the hotel was asked" are the same claim, checkable.
--
-- Filed under 'handoff' would have been the lazy option and would have been
-- wrong: a handoff gives the conversation to a person, and an escalation keeps
-- it while asking somebody a question. Reports that count one as the other
-- would understate how often the AI finished the conversation itself, which is
-- the number the whole product is judged on.

alter table public.agent_actions
  drop constraint if exists agent_actions_result_type_check;

alter table public.agent_actions
  add constraint agent_actions_result_type_check
  check (
    result_type is null or result_type = any (array[
      'task','quote','hold','reservation','message','handoff',
      'service_booking','folio_entry','room','campaign','recommendation',
      'payment_link','escalation','none'
    ])
  );
