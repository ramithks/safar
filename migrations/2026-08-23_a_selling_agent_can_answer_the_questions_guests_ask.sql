-- A selling agent can answer the questions guests actually ask.
--
-- Every agent in production was provisioned before these tools existed, so a
-- code change to the definitions reaches new hotels and none of the fourteen
-- guest and sales agents already running. The definitions are the source of
-- truth for provisioning; `agent_tools` is the source of truth for what an
-- existing agent may do, and only a migration writes the second.
--
-- **Granted to guest and sales only.** The front desk agent works beside a
-- person and does not need to be told what breakfast is; the owner and
-- marketing agents talk to the hotel rather than to guests. A grant nobody
-- needs is still a grant somebody can be talked into using.
--
-- `negotiate_rate`, `get_group_rates`, `get_payment_terms` and
-- `escalate_to_team` go to sales alone. The guest agent answers people the
-- hotel already has a relationship with — its job is a stay that exists, not a
-- price argument about one that does not.

insert into public.agent_tools (agent_id, tool_name, enabled)
select a.id, t.tool_name, true
from public.agents a
cross join (values
  ('get_room_details'),
  ('get_meal_info'),
  ('get_checkin_options'),
  ('get_cancellation_terms'),
  ('get_experiences')
) as t(tool_name)
where a.agent_type in ('guest', 'sales')
on conflict (agent_id, tool_name) do nothing;

insert into public.agent_tools (agent_id, tool_name, enabled)
select a.id, t.tool_name, true
from public.agents a
cross join (values
  ('negotiate_rate'),
  ('get_group_rates'),
  ('get_payment_terms'),
  ('escalate_to_team')
) as t(tool_name)
where a.agent_type = 'sales'
on conflict (agent_id, tool_name) do nothing;
