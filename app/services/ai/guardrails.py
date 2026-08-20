"""
What an agent is allowed to have said.

One function, `check_reply`, run on every agent turn before the words reach a
guest. It is a *deterministic* check on the output, not an instruction in a
prompt, and that distinction is the whole point: a prompt is a request, and this
is a rule.

**The bug it exists for.** Asked "book it", with availability in hand but no
quote, Gemini answered:

    A Courtyard Standard for two from 25 to 27 September will be ₹15,120.
    You can confirm and pay using this link: https://pay.hotel.com/f7538a7c…

Both halves were invented. The price came from nowhere — `get_quote` never ran —
and `pay.hotel.com` is a domain neither we nor the hotel owns. A guest on
WhatsApp would have tapped it. Nothing in the system objected, because every
check we had was on what the agent *did* and this was something it merely said.

Tool results are already tightly controlled; free text was the hole.

**Two rules, both conservative.**

*Links.* Every URL in a reply must appear in a tool result from this same run,
or be on the hotel's own small set of domains. A model cannot invent a payment
page, and cannot repeat one it saw in a guest's message either — that is the
same attack from the other direction.

*Prices.* A currency amount must have been produced by a pricing tool on this
run, or already be in the conversation. Restating "as I said, ₹7,168" is normal
and allowed; conjuring a total nobody computed is not.

A failed check is a **handoff**, never a silent edit. Redacting the link and
sending the rest would leave a guest reading a confident sentence with a hole in
it, and would hide from the hotel that its agent is fabricating. A person picks
this conversation up and the run is recorded as escalated.
"""
import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Tools whose output is allowed to put a number in a guest's hands. Anything
# not here — an availability search, a knowledge lookup — explicitly is not,
# which is why `search_availability` already tells the model in its own output
# not to price from it. This is that instruction made enforceable.
PRICING_TOOLS = frozenset(
    {
        "get_quote",
        "get_stay_options",
        "search_availability",
        "list_services",
        "order_service",
        "get_folio",
        "post_folio_charge",
        "create_payment_link",
        "create_booking_payment_link",
        "get_reservation",
        "find_reservation",
        "get_stay",
        "propose_rate_exception",
        "get_negotiation_policy",
    }
)

_URL = re.compile(r"""(?xi)\b(?:https?://|www\.)[^\s<>"'\)\]]+""")

# ₹1,234 / ₹ 1234.50 / Rs. 1,234 / INR 1234 / 1,234 rupees
_MONEY = re.compile(
    r"""(?xi)
    (?: (?:₹|\bRs\.?|\bINR)\s* (\d[\d,]*(?:\.\d+)?)
      | (\d[\d,]*(?:\.\d+)?) \s* (?:rupees|rs\b) )
    """
)

# Amounts too small to be a price anyone could be defrauded over, and far more
# likely to be a room number, a headcount or a time. Checking them produces
# false handoffs on sentences like "2 rooms" that never mentioned money.
MIN_GUARDED_AMOUNT = 100


@dataclass(frozen=True)
class Verdict:
    ok: bool
    # Why, for the run record and the log. Never shown to a guest.
    reason: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.ok


def _normalise_amount(raw: str) -> str:
    """`1,234.00` and `1234` are the same claim about money."""
    cleaned = raw.replace(",", "")
    try:
        value = float(cleaned)
    except ValueError:
        return cleaned
    return str(int(value)) if value == int(value) else str(value)


def _amounts_in(text: str) -> set[str]:
    found: set[str] = set()
    for match in _MONEY.finditer(text or ""):
        raw = match.group(1) or match.group(2) or ""
        if not raw:
            continue
        normalised = _normalise_amount(raw)
        try:
            if float(normalised) < MIN_GUARDED_AMOUNT:
                continue
        except ValueError:
            continue
        found.add(normalised)
    return found


def _paise_and_rupees(blob: str) -> set[str]:
    """
    Every number a tool result could legitimately have put in a reply.

    Tools speak paise; agents speak rupees. `totalPaise: 716800` is what
    licenses the model to write "₹7,168", so both readings of every number in
    the output are allowed. Deliberately generous — the cost of accepting one
    number a tool happened to return is a sentence that was true anyway, and
    the cost of being strict is a handoff on a correct answer.
    """
    allowed: set[str] = set()
    for raw in re.findall(r"\d[\d,]*(?:\.\d+)?", blob or ""):
        normalised = _normalise_amount(raw)
        allowed.add(normalised)
        try:
            value = float(normalised)
        except ValueError:
            continue
        # The rupee reading of a paise figure, both rounded and truncated,
        # because a model writing ₹1,434 from 143360 has done nothing wrong.
        allowed.add(str(int(value // 100)))
        allowed.add(str(round(value / 100)))
    return allowed


def _hosts_of(urls) -> set[str]:
    hosts: set[str] = set()
    for url in urls:
        if not url:
            continue
        candidate = url if "://" in str(url) else f"https://{url}"
        host = (urlparse(candidate).hostname or "").lower().lstrip(".")
        if host:
            hosts.add(host)
    return hosts


def _host_allowed(host: str, allowed: set[str]) -> bool:
    # Subdomains of an allowed host pass; a host that merely *ends with* the
    # string does not. "evil-travbuddy.in" must not be let through by
    # "travbuddy.in", which a bare `endswith` would do.
    return any(host == a or host.endswith(f".{a}") for a in allowed)


def check_reply(
    reply: str,
    *,
    tool_outputs: list,
    trusted_hosts: set[str] | None = None,
    history: list[dict] | None = None,
) -> Verdict:
    """
    Is this reply backed by something that actually happened?

    `tool_outputs` are this run's `ToolOutcome`s. `history` is the conversation
    so far, which licenses restating a number or a link already sent — and
    **only the assistant's own side of it**, because a guest who pastes a link
    into the chat must not thereby authorise the agent to send it back.
    """
    text = reply or ""
    if not text.strip():
        return Verdict(True)

    blob_parts: list[str] = []
    tools_run: set[str] = set()
    for outcome in tool_outputs or []:
        tools_run.add(getattr(outcome, "tool", "") or "")
        output = getattr(outcome, "output", None)
        if output is not None:
            blob_parts.append(json.dumps(output, default=str))
    tool_blob = " ".join(blob_parts)

    prior_agent_text = " ".join(
        str(m.get("content") or "")
        for m in (history or [])
        if m.get("role") == "assistant"
    )

    # ── links ───────────────────────────────────────────────────────────
    urls = _URL.findall(text)
    if urls:
        allowed_hosts = set(trusted_hosts or set())
        allowed_hosts |= _hosts_of(_URL.findall(tool_blob))
        allowed_hosts |= _hosts_of(_URL.findall(prior_agent_text))

        for url in urls:
            host = (urlparse(url if "://" in url else f"https://{url}").hostname or "").lower()
            if not host or not _host_allowed(host, allowed_hosts):
                return Verdict(
                    False,
                    f"reply contained an unverified link ({host or url[:40]!r})",
                )

    # ── money ───────────────────────────────────────────────────────────
    amounts = _amounts_in(text)
    if amounts:
        if not (tools_run & PRICING_TOOLS):
            # Nothing on this run computed a price. A number already in the
            # conversation may still be restated.
            allowed = _amounts_in(prior_agent_text)
            unbacked = amounts - allowed
            if unbacked:
                return Verdict(
                    False,
                    f"reply quoted {sorted(unbacked)} with no pricing tool on this run",
                )
        else:
            allowed = _paise_and_rupees(tool_blob) | _amounts_in(prior_agent_text)
            unbacked = amounts - allowed
            if unbacked:
                return Verdict(
                    False,
                    f"reply quoted {sorted(unbacked)}, which no tool result contains",
                )

    return Verdict(True)
