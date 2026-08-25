"""
The nights a guest just corrected to, or a question about them.

**Deliberately narrow, and the narrowness is the design.** A date parser that
tries hard is a parser that quietly books somebody into the wrong month. The
rule here is that a date is only read when reading it cannot be wrong in a way
the guest would not immediately notice — and everything else becomes a question
for the model to ask.

**Today is an argument, never a lookup.** Every relative date — "Friday", "the
25th", "next week" — is relative to *the hotel's* today, which is
`clock.business_date(property_id)` and is not the server's. Passing it in rather
than reading a clock here is what makes that explicit: a caller cannot forget the
timezone, because there is no default to forget.

    read()              a correction confident enough to act on
    read().ask          a correction that needs confirming, or was too vague
    read().confidence   how it was arrived at, for the record and the prompt

What is read:

    2026-09-25 to 2026-09-28      ISO, unambiguous
    25 September to 28 September  month named; the next such month
    25th to 28th                  next occurrence, and the model must confirm
    Friday to Monday              next occurrence, and the model must confirm
    3 nights instead              only with a check-in already on the session

What is refused, and why each one would be a wrong booking rather than a wrong
guess:

    5/9                           5 September or May 9th. India writes the
                                  first, half the internet writes the second,
                                  and nothing in the sentence says which.
    "dates change kar do"         an intention with no dates in it.
    "next month"                  a month with no days in it.
    28th to 25th                  backwards, so one of the two is a typo and we
                                  cannot tell which.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

# How the answer was arrived at. Carried onto the session and into the prompt,
# because "we are confident about these nights" and "we think we understood" are
# different things to say to a guest.
EXPLICIT = "explicit"          # a month or a full date was named
INFERRED = "inferred"          # the next occurrence of a day or a weekday
DERIVED = "derived"            # computed from a length and an existing check-in
NONE = "none"

# A stay nobody would book. Both ends are a signal that the sentence was
# misread rather than that the guest wants a year in a hotel room.
_MAX_NIGHTS = 90

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_WEEKDAYS = {
    "monday": 0, "mon": 0, "somvar": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "mangalvar": 1, "wednesday": 2, "wed": 2, "budhvar": 2, "thursday": 3,
    "thu": 3, "thurs": 3, "guruvar": 3, "friday": 4, "fri": 4, "shukravar": 4,
    "saturday": 5, "sat": 5, "shanivar": 5, "sunday": 6, "sun": 6, "ravivar": 6,
}

_ISO = re.compile(r"(?<![\d-])(\d{4})-(\d{2})-(\d{2})(?![\d-])")
_DAY_MONTH = re.compile(
    r"(?<![\d/])(\d{1,2})\s*(?:st|nd|rd|th)?\s+(" + "|".join(_MONTHS) + r")\b",
    re.IGNORECASE)
_MONTH_DAY = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?![\d/])",
    re.IGNORECASE)
_ORDINAL = re.compile(r"(?<![\d/:])(\d{1,2})\s*(?:st|nd|rd|th)(?![\d/])", re.IGNORECASE)
_WEEKDAY = re.compile(r"\b(" + "|".join(_WEEKDAYS) + r")\b", re.IGNORECASE)
_NIGHTS = re.compile(
    r"(?<![a-z0-9])(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"ek|do|teen|char|paanch|panch|chah|chhah|saat|aath|nau|das)\s*"
    r"(?:nights?|raat(?:ein|en)?|din)\b", re.IGNORECASE)
_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "ek": 1, "do": 2, "teen": 3, "char": 4,
    "paanch": 5, "panch": 5, "chah": 6, "chhah": 6, "saat": 7, "aath": 8,
    "nau": 9, "das": 10,
}

# A slash date — the one shape that is genuinely undecidable.
_SLASHED = re.compile(r"(?<![\d/])(\d{1,2})\s*/\s*(\d{1,2})(?:\s*/\s*(\d{2,4}))?(?![\d/])")

# The guest wants to change their dates and has not said what to. An intent with
# no value, which is a question rather than a parse.
_WANTS_CHANGE = re.compile(
    r"(?<![a-z0-9])(?:dates?\s*(?:ko\s*)?(?:change|badal|badlo|badal do|change kar)"
    r"|change\s+(?:the\s+)?dates?|reschedule|postpone|prepone|"
    r"tarikh\s*badal|different\s+dates?|another\s+date)",
    re.IGNORECASE)


@dataclass(frozen=True)
class DateFact:
    check_in: date | None = None
    check_out: date | None = None
    confidence: str = NONE
    ask: str | None = None
    basis: str = ""

    @property
    def usable(self) -> bool:
        """Both ends known, in the right order, and a plausible length."""
        return bool(self.check_in and self.check_out and self.check_in < self.check_out)

    @property
    def needs_confirming(self) -> bool:
        """Read from a day or a weekday with no month — say it back to them."""
        return self.usable and self.confidence == INFERRED


def _to_int(token: str) -> int | None:
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    return _WORD_NUMBERS.get(token)


def _next_with_day(day: int, after: date) -> date | None:
    """
    The next occurrence of this day of the month, on or after `after`.

    **`after` is not always today.** The departure is resolved from the arrival,
    not from today — "25th to 28th" said on the 26th gave 25 September for the
    arrival and 28 *August* for the departure, because both ends were resolved
    from today independently. Backwards, and refused with a question about which
    way round they meant, when the guest had been perfectly clear.
    """
    if not 1 <= day <= 31:
        return None
    for month_offset in range(0, 14):
        year = after.year + (after.month - 1 + month_offset) // 12
        month = (after.month - 1 + month_offset) % 12 + 1
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= after:
            return candidate
    return None


def _next_month_day(month: int, day: int, today: date) -> date | None:
    """The next occurrence of this month-and-day. Year is never stated by guests."""
    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if candidate >= today:
            return candidate
    return None


def _next_weekday(index: int, after: date, *, inclusive: bool = False) -> date:
    """
    The next such weekday.

    **`after` is not always today, and that was a real bug.** Both ends used to
    be resolved from today independently, so "Tuesday to Thursday" said on a
    Tuesday gave next Tuesday for the arrival and *this* Thursday for the
    departure — a backwards stay, which the safety check then refused with a
    question about which way round they meant. The guest had been perfectly
    clear.

    The departure is the next such weekday *after the arrival*. The arrival is
    the next one after today, today excluded, because a guest saying "Friday" on
    a Friday is planning rather than arriving within the hour.
    """
    ahead = (index - after.weekday()) % 7
    if not ahead and not inclusive:
        ahead = 7
    return after + timedelta(days=ahead)


def _explicit_dates(message: str, today: date) -> tuple[list[date], str]:
    """Every date the sentence names outright, in the order written."""
    found: list[tuple[int, date]] = []

    for match in _ISO.finditer(message):
        try:
            found.append((match.start(),
                          date(int(match.group(1)), int(match.group(2)),
                               int(match.group(3)))))
        except ValueError:
            continue
    for match in _DAY_MONTH.finditer(message):
        resolved = _next_month_day(_MONTHS[match.group(2).lower()],
                                   int(match.group(1)), today)
        if resolved:
            found.append((match.start(), resolved))
    for match in _MONTH_DAY.finditer(message):
        resolved = _next_month_day(_MONTHS[match.group(1).lower()],
                                   int(match.group(2)), today)
        if resolved:
            found.append((match.start(), resolved))

    if found:
        found.sort()
        # Deduplicated by value: "25 September to 28 September" matches
        # `_DAY_MONTH` twice and must not become four dates.
        seen, unique = set(), []
        for _, value in found:
            if value not in seen:
                seen.add(value)
                unique.append(value)
        return unique, EXPLICIT
    return [], NONE


def read(message: str, today: date, *, check_in: date | None = None) -> DateFact:
    """
    The nights this message settles on, or a question about them.

    `today` is the hotel's business date and has no default — every relative
    date below is computed from it, and a caller that forgot the timezone would
    silently resolve against the server's day.

    `check_in` is the stay already on the session, and is only used to anchor a
    length: "make it 3 nights instead" means nothing without one.
    """
    text = message or ""
    if not text.strip():
        return DateFact()

    # ── the shape that cannot be read at all ────────────────────────────────
    slashed = _SLASHED.search(text)
    if slashed and int(slashed.group(1)) <= 12 and int(slashed.group(2)) <= 12:
        # 5/9 is 5 September in India and May 9th to half the internet, and
        # nothing in the sentence decides it. A wrong month here is a guest
        # arriving four weeks late.
        return DateFact(
            confidence=NONE,
            ask="Just to be sure I have it right — could you write the month out? "
                "So 5 September rather than 5/9.",
            basis="ambiguous day/month order",
        )

    explicit, how = _explicit_dates(text, today)
    if len(explicit) >= 2:
        return _finish(explicit[0], explicit[1], how, "two dates named outright")
    if len(explicit) == 1:
        nights = _nights_in(text)
        if nights:
            return _finish(explicit[0], explicit[0] + timedelta(days=nights),
                           how, f"a date and {nights} night(s)")
        # One end only. Which end depends on words we would be guessing at.
        return DateFact(
            confidence=NONE,
            ask="And which night would you check out?" if _is_arrival(text)
                else "Which nights did you have in mind?",
            basis="only one date named",
        )

    # ── a length, against the stay already on the session ───────────────────
    nights = _nights_in(text)
    if nights and check_in:
        return _finish(check_in, check_in + timedelta(days=nights), DERIVED,
                       f"{nights} night(s) from the check-in already agreed")
    if nights and not check_in:
        return DateFact(
            confidence=NONE,
            ask="Which night would you like to arrive?",
            basis="a length with nothing to measure it from",
        )

    # ── days of the month, or weekdays ──────────────────────────────────────
    # Each end resolved from the one before it, so a departure is always after
    # its arrival rather than after today. See `_next_with_day`.
    written = [int(m.group(1)) for m in _ORDINAL.finditer(text)]
    ordinals: list[date] = []
    anchor = today
    for day in written:
        resolved = _next_with_day(day, anchor)
        if not resolved:
            continue
        ordinals.append(resolved)
        anchor = resolved
    if len(ordinals) >= 2:
        return _finish(ordinals[0], ordinals[1], INFERRED,
                       "days of the month, resolved to the next such dates")

    named = [_WEEKDAYS[m.group(1).lower()] for m in _WEEKDAY.finditer(text)]
    weekdays: list[date] = []
    anchor = today
    for index, weekday in enumerate(named):
        resolved = _next_weekday(weekday, anchor, inclusive=index > 0)
        weekdays.append(resolved)
        anchor = resolved
    if len(weekdays) >= 2:
        return _finish(weekdays[0], weekdays[1], INFERRED,
                       "weekdays, resolved to the next such days")
    if len(weekdays) == 1 and nights:
        return _finish(weekdays[0], weekdays[0] + timedelta(days=nights), INFERRED,
                       "a weekday and a length")

    # ── an intention with no dates in it ────────────────────────────────────
    if _WANTS_CHANGE.search(text):
        return DateFact(
            confidence=NONE,
            ask="Of course — which nights would you like instead?",
            basis="a request to change dates, with no dates in it",
        )

    return DateFact()


def _is_arrival(text: str) -> bool:
    return bool(re.search(r"check[\s-]?in|arriv|aa\s*raha|aaunga|aauga|pahunch",
                          text, re.IGNORECASE))


def _nights_in(text: str) -> int | None:
    match = _NIGHTS.search(text or "")
    if not match:
        return None
    value = _to_int(match.group(1))
    return value if value and 1 <= value <= _MAX_NIGHTS else None


def _finish(check_in: date, check_out: date, confidence: str, basis: str) -> DateFact:
    """
    The two ends, checked for the things that mean we misread the sentence.

    Backwards is the interesting one. "28th to 25th" is a typo in one of the two
    numbers and there is no way to tell which, so silently swapping them would
    be picking a month for the guest.
    """
    if check_out <= check_in:
        return DateFact(
            confidence=NONE,
            ask="I might have those the wrong way round — which night do you "
                "arrive, and which night do you leave?",
            basis="the dates came out backwards",
        )
    if (check_out - check_in).days > _MAX_NIGHTS:
        return DateFact(
            confidence=NONE,
            ask="That came out as a very long stay — could you tell me the two "
                "dates again?",
            basis="over the plausible length",
        )
    return DateFact(check_in, check_out, confidence, None, basis)
