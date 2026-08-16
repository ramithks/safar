"""
Resolve the caller as a travel agency from the JWT.

    endpoint ──► require_user     (verifies the Supabase JWT)
             ──► require_agency   (resolves b2b_agents rows server-side)

The same shape as `deps/property.require_member`, and deliberately so. A travel
agency is a principal that is not staff: it holds a row in `b2b_agents` rather
than in `partner_members`, and everything else about how it is authenticated is
identical. One auth system, two kinds of membership.

⚠ The agency is ALWAYS derived from `auth.uid()`. No agent-portal route accepts
an `agencyId`, and the property id in a path is checked against the contract in
`b2b.contract_for` before any handler acts on it — a guessed property id 404s
rather than pricing somebody else's contracted rate.

Named `b2b` rather than `agent` because this codebase already has agents: the
AI ones, at `/properties/{id}/agents`. A dependency called `require_agent` next
to them would be read wrong by whoever arrives next.
"""
from fastapi import Depends

from app.deps.auth import require_user
from app.services.pms import b2b


async def require_agency(user=Depends(require_user)) -> b2b.Agency:
    """
    The signed-in travel agency, or 403.

    Suspended contracts come back too. `Contract.require_active()` is what
    refuses a write — an agency that has stopped paying must still be able to
    read what it owes, because that is the list it settles from.
    """
    return await b2b.resolve(str(user.id))
