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

# Every amount an agent says has to appear in something the system actually
# produced on this run. There is deliberately no allow-list of "pricing tools"
# any more.
#
# There was one, and it was wrong in a way that only showed up on the Owner
# Copilot. The set named the tools that price a *stay* — `get_quote`,
# `get_stay_options` and so on — so an owner asking "what did we take last
# week?" got an answer built from `get_revenue`, which was not in the set, and
# every rupee figure in it counted as unbacked. The copilot's reply was
# discarded and the owner was told a colleague would get back to them. About
# their own hotel's revenue.
#
# The rule that was meant all along is simpler and stricter in the place that
# matters: the number must be in a tool result. Which tool produced it is not
# the question — whether anything did, is.

# A stand-in where a real value should be.
#
# The reply that produced this rule, verbatim, to a guest who had just asked to
# book a room:
#
#     Please use this link to pay ₹7,168 and confirm your reservation:
#     [payment link will be here].
#
# The link rule below did not fire, because there was no link — that is the
# whole failure. The model had reached for a tool, been told the action needed
# approval, and composed the sentence it *would* have written if the tool had
# succeeded. A guest cannot tap a placeholder and cannot tell that the hotel
# did not mean to send one.
#
# Deliberately narrow: bracketed or braced text with no closing punctuation
# inside it. Ordinary asides in square brackets are rare in a hotel's replies
# and never contain the words this catches.
_PLACEHOLDER = re.compile(
    r"""(?xi)
    ( \[ [^\]
]{0,60} (link|url|here|insert|placeholder|name|amount|price|todo|xxx) [^\]
]{0,60} \]
    | \{\{ [^}
]{0,60} \}\}
    | < [a-z_ ]{0,40} (link|url|here|insert|name|amount) [a-z_ ]{0,40} > )
    """
)

# Tools that actually put a room in a guest's name, or money in the hotel's.
#
# Nothing else licenses the sentence "you are booked".
CONFIRMING_TOOLS = frozenset({
    "create_booking",
    "create_booking_payment_link",
    "create_payment_link",
    "check_in",
})

# Saying a stay is secured when it is not.
#
# The reply this exists for, sent to a real guest on Instagram after a run
# whose only tools were `get_stay_options` and `get_quote`:
#
#     Okay, aapke 2 Deluxe King Bed rooms 10 September se 14 September tak ke
#     liye book ho gaye hain.        ("your 2 rooms have been booked")
#
# Nothing was booked. A minute later the same agent said those rooms were not
# available. A guest who stops reading at the first message arrives at a hotel
# with no reservation — the single worst thing this product can do, and worse
# than any invented price, because the guest acts on it by travelling.
#
# Hinglish and Hindi are here because the transcript was: an English-only rule
# would have caught nothing on the conversation it was written for.
_CONFIRMED = re.compile(
    r"(?xi)"
    r"("
    r"  book(?:ed|ing)\s+(?:is\s+)?(?:confirm|complete|done|secur)"
    r"| (?:is|are|has\ been|have\ been)\s+(?:now\s+)?(?:booked|reserved|confirmed)"
    r"| reservation\s+(?:is\s+)?confirm"
    r"| rooms?\s+(?:are|is)\s+(?:now\s+)?(?:yours|held|blocked)"
    r"| book\s+ho\s+ga(?:ye|ya|yi)"
    r"| confirm\s+ho\s+ga(?:ye|ya|yi)"
    r"| \u092c\u0941\u0915\s*\u0939\u094b\s*\u0917"
    r"| \u0915\u0928\u094d\u092b\u093c\u0930\u094d\u092e\s*\u0939\u094b\s*\u0917"
    r")"
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
            # `ensure_ascii=False`, and it is load-bearing rather than tidy.
            #
            # The default escapes ₹ to `\u20b9`, and the `9` on the end of that
            # escape fuses with the digits after it: a tool returning "₹5,734"
            # became the text `\u20b95,734`, out of which the number scanner
            # read "20" and "95734" and never "5734". So a payment link with a
            # balance due at the hotel — the single most common shape of a real
            # booking — produced a reply the guardrail could not back, and the
            # guest got a handoff instead of the link they had just asked for.
            #
            # Every amount in this system is written with a rupee sign in front
            # of it. Escaping the one character the whole rule is about was a
            # decision nobody made.
            blob_parts.append(json.dumps(output, default=str, ensure_ascii=False))
    tool_blob = " ".join(blob_parts)

    prior_agent_text = " ".join(
        str(m.get("content") or "")
        for m in (history or [])
        if m.get("role") == "assistant"
    )

    # ── a stand-in for something real ───────────────────────────────────
    stand_in = _PLACEHOLDER.search(text)
    if stand_in:
        return Verdict(
            False,
            f"reply contained a placeholder instead of a real value "
            f"({stand_in.group(0)[:40]!r})",
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

    # ── a booking nobody made ───────────────────────────────────────────
    #
    # Checked before money, because it is the more expensive lie: a guest who
    # believes a wrong price argues at the desk, and a guest who believes a
    # wrong booking arrives with luggage.
    confirmed = _CONFIRMED.search(text)
    if confirmed and not (tools_run & CONFIRMING_TOOLS):
        return Verdict(
            False,
            f"reply said a stay was secured ({confirmed.group(0)[:40]!r}) "
            "with nothing on this run that secures one",
        )

    # ── money ───────────────────────────────────────────────────────────
    #
    # One rule: an amount must be in a tool result from this run, or already in
    # something this agent said earlier in the conversation. Restating "as I
    # mentioned, ₹7,168" is normal; conjuring a total nobody computed is not.
    amounts = _amounts_in(text)
    if amounts:
        allowed = _paise_and_rupees(tool_blob) | _amounts_in(prior_agent_text)
        unbacked = amounts - allowed
        if unbacked:
            return Verdict(
                False,
                f"reply quoted {sorted(unbacked)}, which no tool result contains",
            )

    return Verdict(True)
