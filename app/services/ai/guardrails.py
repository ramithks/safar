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

# Reading a booking is not making one — unless what was read *is* a booking.
#
# `get_booking_status` cannot be in the set above. It runs on every "payment
# hogyi?", and most of the time the honest answer is that nothing has arrived
# yet; a blanket grant would license "aapki booking confirm ho gayi hai" on
# exactly the turn where it is false, which is the sentence this whole rule
# exists to stop.
#
# So the licence comes from the tool's *output* rather than from its name. The
# states below are the ones where a reservation exists and the money for it
# arrived — where "you are booked" is a report, not a promise.
_STATUS_TOOL = "get_booking_status"
_SECURED_STATES = frozenset({"CONFIRMED", "CHECKED_IN", "CHECKED_OUT"})


def _read_a_real_booking(tool_outputs: list) -> bool:
    """Did anything on this run come back holding an actual confirmed stay?"""
    for outcome in tool_outputs or []:
        if getattr(outcome, "tool", "") != _STATUS_TOOL:
            continue
        if not getattr(outcome, "ok", False):
            continue
        output = getattr(outcome, "output", None)
        if isinstance(output, dict) and output.get("state") in _SECURED_STATES:
            return True
    return False

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
    # The perfective *transitive*: "maine aapka room confirm kar diya hai" —
    # I have confirmed it. Found by testing the mood rule below rather than by
    # a transcript: every branch above needs the word "booking" or "rooms"
    # adjacent to the verb, and this is the shape where a guest's own noun sits
    # in between. `kar di` / `kar diya` / `kar liya` and the causative `karwa
    # diya` are all completed action, which is exactly the claim.
    #
    # It cannot collide with the infinitive the tail rule allows: `kar diya` has
    # a `d` where `karni`, `karna` and `karne` have their ending, so the two
    # cannot both match.
    r"| (?:confirm|book)\s+kar\s*(?:wa\s*)?[dl]i(?:ya|ye)?\b"
    r"| \u092c\u0941\u0915\s*\u0939\u094b\s*\u0917"
    r"| \u0915\u0928\u094d\u092b\u093c\u0930\u094d\u092e\s*\u0939\u094b\s*\u0917"
    r")"
)

# ── is the phrase a claim, or only a mention of one? ────────────────────────
#
# `_CONFIRMED` above matches a *phrase*. Whether that phrase is a *claim* is a
# different question, and getting it wrong is expensive in both directions:
# missing a claim tells a guest they have a room they do not have, and mistaking
# a mention for a claim refuses an honest sentence — which is a handoff, at the
# one moment in the conversation where a sale is being closed.
#
# **The second failure was the live one, and it was much worse than it looked.**
# Every honest answer to "is my booking confirmed?" contains the words *booking
# confirm*, because that is what the guest asked about. Measured against the
# rule as it stood, all four of these were refused:
#
#     payment aate hi aapki booking confirm ho jayegi     (it will be, once you pay)
#     booking confirm karne ke liye ₹38,647 pay kar dijiye (to confirm it, pay)
#     aapki booking confirm nahi hui hai abhi tak          (it is NOT confirmed)
#     I will email you once your booking is confirmed      (when it happens)
#
# The third one is a *denial*. The rule that exists to stop the agent claiming a
# booking was refusing the sentence that says there isn't one.
#
# **Why the old shape could not be patched.** It was three regexes arguing:
# `_CONFIRMED` for the phrase, `_NOT_YET` for a hedge anywhere in a ±90-character
# window, and `_PURPOSIVE` for an infinitive in the twelve characters after —
# with `_NOT_YET` gated on a `get_booking_status` call having run this turn. So
# the same sentence was a lie or not a lie depending on which tools the model
# happened to reach for, the window could be satisfied by an unrelated "nahi" a
# few words back, and each new false refusal was answered with another
# alternation. `karni` was the second one this month.
#
# **The shape that works is grammatical mood.** A stay is claimed to be secured
# only when the phrase is *asserted*: indicative, affirmative, about this
# booking, now. Infinitive, prospective, negated and conditional are all mentions
# — whatever tools ran. Hindi puts mood in the verb and the verb after the
# object, so the tail immediately following the phrase decides most of it; the
# rest is an English subordinator heading the same clause. Two bounded lookups
# instead of a window and a gate.

# **The tail: what follows the phrase and unsays it.**
#
# Read against the phrase's own end, not a window, so nothing several words away
# can license it.
#
#   infinitive    "…confirm karne ke liye"  "…confirm karni hai"   in order to / do you want to
#   causative     "…confirm karwana hai"                           to have it confirmed
#   prospective   "…confirm ho jayegi"  "…ho jaye"  "…hoga"        it will be / so that it may be
#   negated       "…confirm nahi hui"  "…confirm na ho"            it has not been
#
# The perfective is deliberately absent, because the perfective is the claim:
# `ho gaya`, `ho gayi`, `kar di hai`, `kar diya`, `hui hai`. Two pairs sit one
# space apart and mean opposite things — `hoga` ("will be") against `ho gaya`
# ("has become"), `karni` ("to do") against `kar di` ("has done") — so the
# patterns below are anchored tightly enough to tell them apart. `hogaya` run
# together is still perfective and still refused: `hog[aeio]` only matches with
# a word boundary after it.
_NOT_ASSERTED = re.compile(r"""(?xi)
    ^\s*
    (?:
        # the infinitive, in all three genders, plain and causative
          kar (?: ne | na | ni ) \b
        | kar [wv] a (?: ne | na | ni ) \b
        # the prospective: jaye / jayega / jayegi / jayenge / jaega / jaayegi
        | (?: ho \s+ )? jaa? y? e (?: g [aeiou] \w* | nge | ) \b
        | (?: ho \s+ )? jaana \b
        # the simple future: hoga / hogi / hoge — but never "ho gaya"
        | hog [aeio] \b
        # the negation, which in Hindi sits between object and verb
        | (?: nahi+n? | naa? ) \b
        | not \b
    )
""")

# **The head: a subordinator that makes the whole clause hypothetical.**
#
# English carries mood in front of the verb — "once your booking is confirmed"
# is a condition, and the phrase itself is indicative. So the clause containing
# the match is searched, from the nearest clause boundary, for a marker that
# subordinates it.
#
# The boundary is a clause break rather than a character count, which is the
# other half of what makes this reliable: in *"When you arrive, your room is
# booked and ready"* the `when` governs a different clause, the comma ends the
# segment, and the claim is correctly refused.
#
# **`ke liye` is deliberately not on this list**, and the reference transcript is
# why. The sentence this whole rule was written for reads
#
#     aapke 2 Deluxe King Bed rooms 10 September se 14 September tak ke liye
#     book ho gaye hain
#
# where `ke liye` means "for" — a date range, not a purpose. Treating it as a
# hypothetical marker would have licensed the single worst sentence the product
# has ever sent. The purposive is *infinitive* + `ke liye`, and the infinitive is
# matched in the tail above, where it cannot be confused with this.
_CLAUSE_BREAK = re.compile(r"[.!?;,\u2013\u2014\n]|\b(?:aur|lekin|magar|but|and)\b", re.I)
_HYPOTHETICAL = re.compile(r"""(?xi)\b(?:
      once | if | unless | whether | when (?:ever)? | after | until | till
    | as\s+soon\s+as | so\s+that | in\s+order
    | agar | jab | jaise\s+hi | jisse | taki | hote\s+hi | kya
)\b""")


# The three moods, and they are three rather than two on purpose.
#
# A first cut of this rule had only "claim" and "not a claim", and it lost a
# guarantee the old code did hold: *"aapki booking confirm nahi hui hai"* — your
# booking is not confirmed — is every bit as much a statement about a stay as
# the affirmative, and an agent that has not looked does not know it. Saying it
# to a guest who *has* paid sends them to re-pay or to panic.
#
# So a denial is asserted too; it simply needs a different licence. The
# affirmative needs a tool that secures a stay. The denial needs a lookup that
# came back saying there isn't one. Only the hypothetical needs nothing, because
# it makes no claim about the present state at all.
CLAIMED = "claimed"          # "aapki booking confirm ho gayi hai"
DENIED = "denied"            # "aapki booking confirm nahi hui hai"
HYPOTHETICAL = "hypothetical"  # "…confirm ho jayegi" / "…karne ke liye" / "agar…"

# The negation, split out of the tail rule so it can be licensed separately.
# Hindi puts it between the object and the verb, which is where the tail looks.
_NEGATED = re.compile(r"(?xi) ^\s* (?: nahi+n? | naa? | not ) \b")


def _mood(text: str, match: re.Match) -> str:
    """
    Is this occurrence a claim, a denial, or neither?

    The clause head is read before the tail: *"agar booking confirm nahi hui to
    batayein"* is a condition, not a denial, and the `agar` that makes it one
    governs the whole clause.
    """
    head = text[:match.start()]
    breaks = list(_CLAUSE_BREAK.finditer(head))
    clause = head[breaks[-1].end():] if breaks else head
    if _HYPOTHETICAL.search(clause):
        return HYPOTHETICAL

    tail = text[match.end():match.end() + 24]
    if _NEGATED.match(tail):
        return DENIED
    if _NOT_ASSERTED.match(tail):
        return HYPOTHETICAL
    return CLAIMED


def _state_was_looked_up(tool_outputs: list) -> bool:
    """Did a status lookup this turn come back saying the stay is not secured?"""
    for outcome in tool_outputs or []:
        if getattr(outcome, "tool", "") != _STATUS_TOOL:
            continue
        if not getattr(outcome, "ok", False):
            continue
        output = getattr(outcome, "output", None)
        if isinstance(output, dict) and output.get("state") not in _SECURED_STATES:
            return True
    return False


def _unlicensed_claim(text: str, tool_outputs: list, tools_run: set) -> tuple[str, str] | None:
    """
    The first statement about a stay that nothing on this run backs.

    Returns the mood and the phrase, or None when every occurrence is either
    licensed or makes no claim.
    """
    licensed_affirmative = bool(tools_run & CONFIRMING_TOOLS) or _read_a_real_booking(tool_outputs)
    licensed_denial = _state_was_looked_up(tool_outputs)

    position = 0
    while True:
        found = _CONFIRMED.search(text, position)
        if found is None:
            return None
        mood = _mood(text, found)
        if mood == CLAIMED and not licensed_affirmative:
            return CLAIMED, found.group(0)
        if mood == DENIED and not licensed_denial:
            return DENIED, found.group(0)
        position = found.end()


_URL = re.compile(r"""(?xi)\b(?:https?://|www\.)[^\s<>"'\)\]]+""")


# ── what a guest is never shown ─────────────────────────────────────────────
#
# Everything above this line is about a reply being *false*. This is about a
# reply being true and still wrong to send.
#
# The transcript that produced the rule. A guest gave their name and was
# answered:
#
#     Sahil ji, booking aage badhane ke liye mujhe ek quote ID ki zarurat hai.
#     ("to take the booking forward I need a quote ID")
#
# The guest does not have a quote ID. Nobody outside this system has ever had
# one. The model was reading its own tool schema aloud, and a guest reading that
# concludes either that they have lost something they were sent, or that they
# are talking to a machine that has stopped working. Both end the sale.
#
# The other half is failure mode ⑥: an exception string reaching the guest
# through the model's paraphrase. `registry.execute` no longer hands the model
# a Python traceback, which is the fix; this is the backstop for the case where
# a tool author writes one into a message themselves.
#
# **Deliberately narrow, and each pattern earned its place.** A guardrail that
# fires on ordinary prose is one somebody switches off. So: no bare word lists,
# no "id" on its own — "aapki booking ID TB-5CECKV hai" is exactly right and
# must pass.

_INTERNALS = (
    # A raw uuid. Every internal identifier in this system is one, and none of
    # them means anything to a guest. `TB-5CECKV` is not a uuid and is the
    # reference a guest is *supposed* to be given.
    ("an internal id",
     re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)),

    # Asking a guest for something only the system holds. The verb matters:
    # *telling* someone their booking reference is right, asking them for a
    # quote id is the failure. Hinglish and Hindi because the transcript was.
    ("asked the guest for an internal identifier",
     re.compile(r"""(?xi)
        (?: (?:need|give\ me|provide|share|send\ me|what\ is|enter|tell\ me)
            [^.!?\n]{0,40}
          | (?:zarurat|chahiye|bhej|batao|de\ do) [^.!?\n]{0,40} )?
        \b (?: quote | rate[\ -]?plan | room[\ -]?type | conversation | property
              | reservation | agent | tool | session )
        [\ _-]? id \b
        """)),

    # The names of things inside the machine. `send_room_photos` is a tool;
    # `folio_entries` is a table. A guest should never learn either exists.
    ("a tool or table name",
     re.compile(r"(?xi) \b [a-z]{3,}(?:_[a-z]{2,}){1,3} \b (?! \.[a-z] )")),

    # The stack, and the vendor.
    ("an internal error",
     re.compile(r"""(?xi)
        \b (?: traceback | exception | stack\ trace | null\ pointer
              | supabase | postgrest | postgres | psycopg | httpx | vertex
              | gemini | celery | redis | 5\d{2}\ (?:error|internal)
              | internal\ server\ error ) \b
        """)),

    # The instructions themselves. §38: the model must not be talkable into
    # reciting them, and this is the deterministic half of that.
    ("its own instructions",
     re.compile(r"""(?xi)
        \b (?: system\ (?:prompt|instruction|message)
              | my\ instructions\ (?:are|say)
              | ground\ rules
              | you\ are\ a\ hotel's\ own\ assistant ) \b
        """)),
)

# Snake_case that is allowed to appear, because it is a word a hotel uses rather
# than a name from inside the system. Kept as an explicit list so that adding to
# it is a decision somebody makes on purpose.
_SNAKE_ALLOWED = frozenset({
    "check_in", "check_out", "check_in_time", "check_out_time",
    "wi_fi", "air_conditioning", "room_service", "bed_and_breakfast",
})


def _internal_leak(text: str) -> tuple[str, str] | None:
    """
    The first thing in this reply a guest should not be reading, if any.

    **Links are masked out before the scan, and that is deliberate.** A URL's
    path is not prose: it was built by a system, and whether it is a link this
    guest may be sent is the *links* rule's question, two checks below — an
    address whose host no tool produced is refused there regardless of what its
    path spells.

    Without the mask, the snake-case rule refused the single most important
    message in a sale. Every payment link this product issues is
    `/pay/order_TTNlZOqsCGlaWH` — a Razorpay order id, `order_` and all — and
    `[a-z]{3,}_[a-z]{2,}` matches it. The guest asking to pay was handed to a
    human instead. It survived because the replay raises the link itself when
    the model does not, so the suite kept driving the money path around the bug
    it was meant to find.
    """
    text = _URL.sub(" \u2026 ", text or "")
    for label, pattern in _INTERNALS:
        for match in pattern.finditer(text):
            found = match.group(0)
            if label == "a tool or table name" and found.lower() in _SNAKE_ALLOWED:
                continue
            return label, found
    return None


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
    # string does not. "evil-safar.example.com" must not be let through by
    # "safar.example.com", which a bare `endswith` would do.
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

    # ── the machine showing through ─────────────────────────────────────
    #
    # Checked before links and money because it is the one rule that fires on a
    # reply which is entirely *true*. A guest asked for a quote id is being told
    # something accurate about how the system works and useless to them.
    leak = _internal_leak(text)
    if leak:
        label, found = leak
        return Verdict(False, f"reply exposed {label} ({found[:40]!r})")

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
    # Only the occurrences that state something about the stay *now* — see
    # `_mood`. An infinitive, a future or a conditional is a mention of a
    # booking rather than an assertion that one exists, and needs no licence:
    # no arrangement of tool calls can make "payment aate hi booking confirm ho
    # jayegi" into a statement that the guest already has a room.
    unlicensed = _unlicensed_claim(text, tool_outputs, tools_run)
    if unlicensed:
        mood, phrase = unlicensed
        return Verdict(
            False,
            f"reply said a stay was secured ({phrase[:40]!r}) "
            "with nothing on this run that secures one"
            if mood == CLAIMED else
            f"reply said a stay was not secured ({phrase[:40]!r}) "
            "without looking it up on this run",
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
