"""
"Make it warmer." — a model rephrasing words the hotel already wrote.

    existing text  ─┐
                    ├─► model ─► candidate ─► gates ─► shown to the owner
    a tone          ─┘                          │
                                                └─► refused, original kept

**The model may rephrase. It may not add.** Everything else on a HostOS website
is composed from columns by `website_compose`, with no model anywhere in the
path, because an invented amenity is a promise the hotel has to keep and has no
digit in it for `claims.py` to catch. This module is the single exception, and
it is narrow on purpose: there is always existing text, the instruction is
always "say this differently", and three gates stand between the answer and the
page.

    1. It must not invent a figure. `claims.check` refuses any number the
       source text did not already contain.
    2. It must not invent a facility. A candidate naming a pool, a spa, an
       airport shuttle or a restaurant that the source text does not name is
       refused outright — this is the gate that matters, because it is the one
       failure that survives a proofread by somebody who knows their own hotel
       and is reading for tone.
    3. It must stay roughly the length that was asked for.

**A refusal is not an error.** The owner keeps the words they had and is told
the rewrite was not used. A model that fails this gate has produced a sentence
about a hotel that does not exist, and the correct response is to throw it away
quietly rather than to show it with a warning nobody reads.

Nothing here writes. The endpoint hands the candidate back and the owner
presses Save — the same as if they had typed it.
"""
import logging
import re

from fastapi import HTTPException, status

from app.services.marketing import claims
from app.services.vertex import generate_json

logger = logging.getLogger(__name__)

# What an owner may ask for. Closed, because the instruction is concatenated
# into a prompt and free text there is a hotelier — or anybody who reaches this
# endpoint — writing the prompt.
TONES: dict[str, str] = {
    "luxurious": "more refined and upmarket, without inventing luxuries",
    "welcoming": "warmer and more personal, as if written by the owner",
    "shorter": "about half as long, keeping every fact",
    "conversions": "more persuasive about booking directly, still factual",
    "international": "clearer for a guest whose first language is not English; "
                     "shorter sentences, no idioms",
    "simpler": "plainer and easier to read",
}

# Facilities a rewrite is not allowed to introduce.
#
# Not an exhaustive list of things that can be invented — that list is
# unbounded — but the ones a model reaches for unprompted when told to make a
# hotel sound nicer. Each is checked as "named in the candidate and not in the
# source", so a hotel that genuinely has a pool can still have its pool
# sentence rewritten.
_INVENTABLE = (
    r"pool", r"spa\b", r"gym|fitness", r"airport (shuttle|transfer|pick)",
    r"rooftop", r"beach", r"restaurant", r"bar\b", r"breakfast",
    r"free wi-?fi|complimentary wi-?fi", r"parking", r"butler|concierge",
    r"sea view|ocean view|mountain view|lake view",
    r"award|rated|star hotel|\d ?star", r"michelin",
)

MAX_SOURCE = 2000


def _mentions(text: str) -> set[str]:
    lower = (text or "").lower()
    return {pattern for pattern in _INVENTABLE if re.search(pattern, lower)}


def _schema() -> dict:
    return {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }


def check(candidate: str, source: str) -> tuple[bool, str | None]:
    """
    Whether this rewrite may be shown. `(ok, why not)`.

    Public because the tests drive it directly: the gates are the feature, and
    a test that has to call a model to exercise them is a test nobody runs.
    """
    text = (candidate or "").strip()
    if not text:
        return False, "empty"

    # 1 — figures. The source's own numbers are the authorised set, so a
    # rewrite may keep "₹3,200" and may not discover "₹2,800".
    #
    # Set difference rather than `claims.check`, which answers a different
    # question: it validates a campaign body against a campaign's *offer*, and
    # there is no offer here. The authorised set is simply what the hotelier's
    # own sentence already committed to.
    if _figures(text) - _figures(source):
        return False, "invented a number the original did not contain"

    # The absolute claims `claims` refuses by name — "guaranteed", "cheapest",
    # "last chance". Reused rather than restated, because a promise that is
    # unsendable in an ad is unpublishable on a home page for the same reason.
    lower = text.lower()
    forbidden = [word for word in claims._FORBIDDEN if word in lower and word not in source.lower()]
    if forbidden:
        return False, f"made a promise the original did not: {forbidden[0]}"

    # 2 — facilities.
    added = _mentions(text) - _mentions(source)
    if added:
        return False, "mentioned something the original did not"

    # 3 — length. A "shorter" that comes back longer, or any rewrite that
    # triples the text, has stopped rephrasing and started writing.
    if len(text) > max(240, len(source) * 2):
        return False, "much longer than the original"

    return True, None


# Every digit-run in the text, however it is punctuated or spelled.
#
# Broader than `claims._numbers`, which only looks at money, percentages and
# ISO dates: a rewrite that turns "check-in is from 2 pm" into "check-in is
# from 11 am" states a fact nobody gave it, and there is no currency symbol
# anywhere near it.
_DIGITS = re.compile(r"\d[\d,.:]*")


def _figures(text: str) -> set[str]:
    return {
        m.rstrip(".,:").replace(",", "")
        for m in _DIGITS.findall(text or "")
        if m.rstrip(".,:")
    }


async def rewrite(*, text: str, tone: str, hotel: str, field: str) -> str:
    """
    One rephrasing of one field, or an HTTP 422 saying it could not be done.

    Raises rather than returning the original silently: the caller is a button
    an owner pressed, and a button that appears to do nothing is worse than one
    that says "we could not do that without changing what your hotel offers".
    """
    source = (text or "").strip()
    if not source:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "There is nothing to rewrite yet. Generate or write something first.",
        )
    if tone not in TONES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Unknown style. One of: {', '.join(TONES)}.",
        )

    source = source[:MAX_SOURCE]
    prompt = (
        "You are editing one piece of text on a hotel's own website.\n\n"
        f"HOTEL: {hotel}\n"
        f"FIELD: {field}\n"
        f"CURRENT TEXT:\n{source}\n\n"
        f"Rewrite it to be {TONES[tone]}.\n\n"
        "RULES — these are absolute:\n"
        "- Use only facts that are already in the current text. Do not add a "
        "facility, amenity, view, award, rating, or service that is not "
        "already mentioned.\n"
        "- Do not add or change any number, price, percentage, time or date.\n"
        "- Do not invent anything about the location or the surroundings.\n"
        "- If you cannot improve it without adding something, return it "
        "almost unchanged.\n"
        "- Plain sentences. No markdown, no emoji, no headings.\n\n"
        'Answer as JSON: {"text": "..."}'
    )

    try:
        answer = await generate_json(prompt, tier="muscle", response_schema=_schema())
    except Exception:  # noqa: BLE001 — a model outage is not a 500 on a hotel's page
        logger.exception("[website-rewrite] model call failed")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "We couldn't rewrite that just now. Your text is unchanged — please try again.",
        ) from None

    candidate = str((answer or {}).get("text") or "").strip()
    ok, why = check(candidate, source)
    if not ok:
        logger.warning("[website-rewrite] refused (%s) field=%s tone=%s", why, field, tone)
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "That rewrite would have said something about your hotel that isn't in your "
            "own words, so we haven't used it. Your text is unchanged.",
        )
    return candidate
