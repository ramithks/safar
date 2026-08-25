"""
What the guest is *doing*, as distinct from what they are talking about.

── the failure this exists for ──────────────────────────────────────────────

The model matrix has one scenario chosen to require no business action at all:

    "Breakfast kitne baje tak milta hai?"

It came back as a required action on 4 of 4 runs on Gemini 2.5 and 4 of 4 on
Gemini 3.x. Identical on both families is what a system bug looks like when it
is being charged to a model. `kitne` is a quantity word, `_QUOTE` looks for
quantity words, and a question about the hour was read as a request for a price
— which carried the entire pricing tool set into a turn about breakfast, and
invited the model to quote a stay nobody asked to price.

Stripping `kitne baje` fixed that sentence. It did not fix the shape, and the
shape is what matters: every requirement in `required_actions` answers

    does this message contain a business-related keyword?

when the question it needs to answer is

    does this guest require a business action?

Those differ on the entire informational half of a hotel's inbox — the half a
guest sends most — because the *topic* of a hotel question is almost always a
business noun. Breakfast, check-out, parking, the pool and the cancellation
policy are all things with prices and rules attached. What separates "what time
is breakfast" from "can you add breakfast" is not the noun. It is the verb.

── the six categories ───────────────────────────────────────────────────────

  INFORMATIONAL   a fact about the property.  "What time is breakfast?"
                  Owes nothing. No tool, no lookup, no quote.

  STATE_LOOKUP    a fact about *this guest's own* booking.
                  "Is breakfast included in my reservation?"
                  Owes a read of their booking and nothing else.

  TRANSACTIONAL   wants something priced, held or acquired.
                  "Can you add breakfast?"  "5 kamre chahiye"

  MUTATION        wants an existing booking changed or ended.
                  "Remove breakfast from my booking"

  NEGOTIATION     wants a better price than the one on offer.
                  "Can you give breakfast free?"

  ESCALATION      wants a person.

They are speech acts, not topics, which is why the same noun appears in all
six. The categories are ordered: a message is whichever comes *first* in
`_ORDER`, because a guest who asks two things at once is owed the more
consequential answer. "Breakfast free kar do aur timing bhi bata dena" is a
negotiation that also asks the time, and answering only the time would be the
system choosing the cheaper half.

── how it is used ───────────────────────────────────────────────────────────

`required_actions.detect` runs its own detectors and then filters what they
found through `ALLOWS`. That order matters: the detectors stay exactly as they
are, each still responsible for recognising its own case, and this decides
which of their findings the *turn* can legitimately owe. A new detector added
tomorrow is gated by construction rather than by its author remembering to ask.

ESCALATION is permitted from every category and is never filtered. A guest
asking for a person gets one whatever else the sentence was doing.
"""
import re

INFORMATIONAL = "INFORMATIONAL"
STATE_LOOKUP = "STATE_LOOKUP"
TRANSACTIONAL = "TRANSACTIONAL"
MUTATION = "MUTATION"
NEGOTIATION = "NEGOTIATION"
ESCALATION = "ESCALATION"

CATEGORIES = (INFORMATIONAL, STATE_LOOKUP, TRANSACTIONAL, MUTATION,
              NEGOTIATION, ESCALATION)

# Most consequential first. A message is the first category that claims it.
_ORDER = (ESCALATION, NEGOTIATION, MUTATION, TRANSACTIONAL, STATE_LOOKUP,
          INFORMATIONAL)


# ── the nouns that are only ever informational ──────────────────────────────
#
# Facilities and policies. Every one of them has a price somewhere in the
# hotel's system, which is exactly why the noun cannot be what decides.
_AMENITY = re.compile(
    r"(?<![a-z0-9])(?:"
    r"breakfast|nashta|naashta|brunch|lunch|dinner|khana|"
    r"wi-?fi|wifi|internet|password|"
    r"parking|park karne|"
    r"pool|swimming|"
    r"restaurant|cafe|bar|kitchen|"
    r"gym|spa|sauna|massage|"
    r"laundry|dhulai|iron|"
    r"pets?|dogs?|cats?|kutta|"
    # With a space too: "When can I check in?" is the commonest phrasing
    # of the commonest question, and only the hyphenated and closed-up
    # forms were listed.
    r"check[- ]?in|checkin|check[- ]?out|checkout|"
    r"ac|a\.c\.|air ?conditioning|heater|geyser|"
    r"lift|elevator|balcony|terrace|garden|view|"
    r"towel|toiletries|shampoo|luggage|storage|locker|cloak ?room|"
    r"reception|front ?desk|lobby|room ?service|housekeeping|"
    # Places near the hotel. A guest asking where the nearest ATM is has
    # asked the front desk a question, not the booking engine.
    r"shuttle|cab|taxi|airport|station|atm|bank|pharmacy|chemist|"
    r"hospital|market|mall|temple|beach|nearby|near by|paas mein|aas ?paas|"
    # Things a guest asks a front desk that have nothing to do with a
    # booking. "tomorrow ka weather kaisa hai?" was forcing an availability
    # search, because "tomorrow" is a date.
    r"smoking|alcohol|weather|mausam|temperature|garmi|thand|barish|rain|"
    # The *terms*, which are a property fact. Cancelling is a mutation and
    # is matched by `_CHANGES_A_BOOKING`; asking what it would cost in
    # general is a question the front desk answers.
    r"cancellation policy|cancellation charge|cancellation charges|"
    r"cancellation fee|cancellation ke charge|refund policy|policy|"
    r"policies|niyam|terms|"
    r"address|location|located|pata|kahan hai|how to reach|reach the|directions|hotel"
    r")(?![a-z0-9])",
    re.IGNORECASE,
)
_AMENITY_HI = re.compile(
    r"नाश्ता|खाना|पार्किंग|वाईफ़ाई|वाई-फ़ाई|पूल|रेस्टोरेंट|जिम|पता|समय|बजे|नियम|"
    r"लिफ्ट|कमरा सेवा|धुलाई|सामान|चेक-इन|चेक-आउट|चेक इन|चेक आउट"
)

# ── the speech acts ─────────────────────────────────────────────────────────
#
# A question about a fact. Deliberately includes the clock words that started
# all this: `kitne baje` belongs here and nowhere else.
_ASKS_A_FACT = re.compile(
    r"(?<![a-z0-9])(?:"
    r"what time|when|where|how do|how does|how can i get|is there|are there|"
    r"is the|is it|is your|are the|are they|does it|does the|do you|"
    r"can i get|can i use|how far|how many|khula|khuli|band|bandh|"
    r"do you have|do you offer|does the hotel|what are|what is|whats|what's|"
    r"kitne baje|kitni der|kab tak|kab se|kab|kahan|kaha|kidhar|"
    r"kya hai|kya h|hai kya|hai\?|milta hai|milti hai|milega|milegi|"
    r"hota hai|hoti hai|kaise|batao|bata do|bataiye|"
    r"available hai|allowed hai|allowed h"
    r")(?![a-z0-9])",
    re.IGNORECASE,
)
_ASKS_A_FACT_HI = re.compile(r"कितने बजे|कब|कहाँ|कहां|क्या है|क्या\?|है क्या|कैसे|बताइए|बताओ")

# **A fact about *my* booking**, which is a different question with a different
# answer. The possessive is the whole signal: "is breakfast included" is a rate
# question about the property, "is breakfast included in my booking" is a
# lookup against one reservation.
# **The possessive has to attach to a booking, not to a noun.**
#
# This was a bare list of possessives, and "Can I bring my dog?" came back as a
# STATE_LOOKUP against the guest's reservation — a policy question about the
# hotel, routed to a read of a booking, because it contained the word "my".
# Guests own dogs, cars, children and luggage, and none of those are a
# reservation.
#
# So the possessive is only a booking reference when a booking word follows it.
_BOOKING_NOUN = (r"booking|reservation|stay|room|rooms|rate|package|"
                 r"quote|trip|dates?|kamra|kamre|booking id")
_MINE = re.compile(
    r"(?<![a-z0-9])(?:"
    rf"(?:my|our|mera|meri|mere|hamara|hamari|hamare|apna|apni)\s+(?:{_BOOKING_NOUN})|"
    r"i booked|we booked|i have booked|maine book|humne book|"
    r"i've booked|we've booked"
    r")(?![a-z0-9])",
    re.IGNORECASE,
)
_MINE_HI = re.compile(r"(?:मेरा|मेरी|मेरे|हमारा|हमारी|हमारे)\s*(?:बुकिंग|कमरा|कमरे|रिज़र्वेशन|रेट)")

# Wants an existing booking changed or ended. The verbs are transitive and act
# on something that already exists.
_CHANGES_A_BOOKING = re.compile(
    r"(?<![a-z0-9])(?:"
    r"cancel|cancell?ation|refund|"
    r"remove|delete|drop|take off|hata do|hata dijiye|nikal do|"
    r"change|modify|reschedule|postpone|prepone|move|shift|swap|"
    r"badal|badal do|badalna|change kar|extend|shorten|"
    r"upgrade|downgrade|"
    r"update my|amend"
    r")(?![a-z0-9])",
    re.IGNORECASE,
)
_CHANGES_A_BOOKING_HI = re.compile(r"रद्द|कैंसिल|बदल|हटा")

# Wants a better price than the one on offer.
_ASKS_FOR_LESS = re.compile(
    r"(?<![a-z0-9])(?:"
    r"discount|cheaper|cheap|less|lower|reduce|"
    r"free|complimentary|muft|mufat|"
    r"best price|best rate|best you can|"
    r"kam kar|kam karo|kam kijiye|kam ho|thoda kam|sasta|"
    r"offer|deal|bargain|negotiate|"
    # **The polite English haggle, which is most of them.** "That's a bit
    # above our budget — can you do anything on the rate?" matched nothing
    # here, so the turn read as a quote request; the model ran negotiate_rate,
    # correctly, and the evaluation scored it as a required action that never
    # ran — on 10 of 10 runs across *both* model families. Identical on both
    # is the signature of a system bug being charged to a model, and this is
    # the third time that signature has appeared in this work.
    #
    # An indirect ask names the price as a problem rather than naming a
    # discount: over budget, too expensive, a stretch, more than we hoped.
    r"above (?:our|my|the) budget|over (?:our|my|the) budget|out of (?:our|my) budget|"
    r"beyond (?:our|my) budget|bit (?:much|steep|high|pricey)|too (?:much|expensive|"
    r"pricey|steep|high)|a stretch|more than (?:we|i) (?:hoped|wanted|expected)|"
    r"can you do anything|anything you can do|any (?:flexibility|room|leeway)|"
    r"flexible on|meet us|work with us|help us out|"
    r"waive|waiver|"
    # A guest naming their own figure is haggling, whatever else the
    # sentence does. "4000 mein ho jayega?" is an offer.
    # A guest naming their own figure is haggling, whatever else the sentence
    # does. "4000 mein ho jayega?" is an offer with a question mark on it.
    # **"Can you do 10% off?" is the commonest way to ask in English** and
    # matched nothing here — `off` was not a discount word, `do` was not a
    # request, and the whole sentence came back TRANSACTIONAL. Found by the
    # first-guest journey, on turn six.
    r"knock off|take off|come down|kuch to kam|"
    r"kar do na|de do na|de dijiye na|"
    r"mein ho jayega|me ho jayega|mein ho jaye|mein kar do|mein de do"
    r")(?![a-z0-9])",
    re.IGNORECASE,
)
_ASKS_FOR_LESS_HI = re.compile(r"छूट|सस्ता|कम कर|मुफ़्त|मुफ्त")

# **"Can you do 10% off?" is the commonest way to ask in English**, and it
# matched nothing: `off` is not a discount word on its own, `do` is not a
# request, and the whole sentence came back TRANSACTIONAL. Found by the
# first-guest journey, on turn six, against a hotel with real negotiation rules.
#
# Its own pattern rather than another branch of `_ASKS_FOR_LESS`, because that
# one is wrapped in `(?<![a-z0-9])` and the character before a `%` is a digit —
# so every percentage form failed the lookbehind and only the spelled-out
# "percent" worked. Fighting the wrapper would have made both patterns harder
# to read than having two.
#
# The number has to be *next to* the asking. A bare percentage is not a
# discount request: "18% GST lagega kya?" is a question about tax.
_PERCENT_OFF = re.compile(
    r"\d+\s*(?:%|percent|per ?cent)\s*(?:off|discount|down|kam|less|chh?oo?t)",
    re.IGNORECASE,
)

# **"How many rooms are free tonight?" is an availability question.**
#
# A real guest message, read as a haggle because `free` is in the list above.
# The word does both jobs in English and the neighbouring noun settles it: a
# room is free when nobody is in it, and breakfast is free when nobody pays for
# it. Only the room sense is excluded here — "free breakfast" and "free
# upgrade" stay negotiations.
_FREE_MEANS_VACANT = re.compile(
    r"(?<![a-z0-9])(?:"
    r"(?:rooms?|kamre|kamra|suites?|villas?|beds?|dates?|nights?|slots?|tables?)"
    r"\s+(?:\w+\s+){0,2}?free"
    r"|free\s+(?:rooms?|kamre|kamra|suites?|villas?|beds?|dates?|nights?|slots?|tables?)"
    r")(?![a-z0-9])",
    re.IGNORECASE,
)

# Wants something priced, held or acquired. Note `add`, which is what separates
# "can you add breakfast" from "what time is breakfast".
_WANTS_SOMETHING = re.compile(
    r"(?<![a-z0-9])(?:"
    r"book|booking karni|booking karna|reserve|reservation chahiye|"
    # `include` but not `included` — see `_IS_IT_INCLUDED`. "include
    # breakfast" is an instruction; "breakfast included hai?" is a question
    # about a rate plan, and the participle is the whole difference.
    r"add|include(?!d)|lagwa|lagwana|karwa do|karwana|"
    r"want|need|chahiye|chahiye tha|chaahiye|lena hai|karna hai|"
    r"available|availability|khali hai|khali h|vacancy|"
    r"price|rate|rates|tariff|cost|charge|charges|quote|"
    r"kitna|kitni|kitne(?! baje)|daam|keemat|kiraya|"
    r"how much|per night|total|"
    r"pay|payment|paisa|paise bhejo|link bhejo|"
    r"rooms?|kamre|kamra|suite|"
    r"stay|nights?|raat"
    r")(?![a-z0-9])",
    re.IGNORECASE,
)
_WANTS_SOMETHING_HI = re.compile(r"चाहिए|बुक|कमरा|कमरे|कितना|कितने(?! बजे)|दाम|कीमत|किराया")

# **"Is breakfast included?" is a question about a rate, not about a building.**
#
# It has no possessive in it and is still not a property fact: what a rate plan
# contains is a lookup, and answering it from the model's memory of the hotel
# is the same class of mistake as quoting a price from memory. So the participle
# routes to STATE_LOOKUP whether or not the guest said "my".
_IS_IT_INCLUDED = re.compile(
    r"(?<![a-z0-9])(?:included|include karke|include breakfast|rate include|shamil|included hai|"
    r"comes with|part of the (?:rate|price|package)|milta hai kya)"
    r"(?![a-z0-9])",
    re.IGNORECASE,
)
_IS_IT_INCLUDED_HI = re.compile(r"शामिल|शामिल है")

_ASKS_FOR_A_PERSON = re.compile(
    r"(?<![a-z0-9])(?:"
    r"human|person|manager|owner|staff|receptionist|"
    r"insaan|aadmi|banda|kisi se baat|kisi se bat|"
    r"talk to someone|speak to someone|real person|"
    r"call me|call kar|callback|call back"
    r")(?![a-z0-9])",
    re.IGNORECASE,
)


# **The mood, which is what the business vocabulary kept mistaking.**
#
# "Parking free hai?" and "parking free kar do" share the word `free`; only the
# second is a haggle. "Cancellation policy kya hai?" and "meri booking cancel
# kar do" share the word `cancel`; only the second cancels anything. A question
# about a fact carries no request in it, and the presence of a *request* is the
# thing to test for — not the presence of the noun.
#
# This is the general form of the bug that started the module. Stripping
# `kitne baje` fixed one sentence; a hotel's whole informational inbox is built
# out of business nouns, and the only thing separating it from the
# transactional half is whether the guest asked for something.
_REQUESTS_SOMETHING = re.compile(
    r"(?<![a-z0-9])(?:"
    r"can you|could you|would you|will you|please|plz|pls|"
    r"i want|i need|i'd like|i would like|we want|we need|"
    r"kar do|kar dijiye|kar den|kardo|karo|kariye|de do|de dijiye|dedo|"
    r"chahiye|chaahiye|karna hai|karni hai|lena hai|bhejo|bhej do|bhej dijiye|"
    r"book|reserve|add|remove|cancel my|cancel karna|cancel kar|"
    r"change my|update my|arrange|organise|organize"
    r")(?![a-z0-9])",
    re.IGNORECASE,
)
_REQUESTS_SOMETHING_HI = re.compile(r"कर दो|कर दीजिए|चाहिए|कीजिए|दीजिए|भेजो")


# **Two general shapes, which between them carry most of a real inbox.**
#
# The vocabulary above lists particular phrasings and will always be behind the
# language; these two are structural. An English sentence that opens with an
# interrogative auxiliary is a question — "Is breakfast served in the room?",
# "Are pets allowed?", "Do you have room service?" — and a message that ends in
# a question mark is one in every language this product sees, which is what
# catches "breakfast buffet hai ya a la carte?" and "पार्किंग है क्या?" without
# enumerating Hinglish sentence-final particles forever.
#
# Neither can suppress anything on its own. Both are read only inside the
# amenity-and-no-request branch, so "Can you add breakfast?" is still a request
# and "5 kamre chahiye?" is still a booking.
_OPENS_A_QUESTION = re.compile(
    r"^\s*(?:is|are|was|were|do|does|did|has|have|can|could|may|might|will|would|"
    r"what|when|where|which|how|why|who|whose)\b",
    re.IGNORECASE,
)


def _asks_a_question(text: str) -> bool:
    return bool(
        _ASKS_A_FACT.search(text)
        or _ASKS_A_FACT_HI.search(text)
        or _OPENS_A_QUESTION.match(text)
        or text.rstrip().endswith(("?", "？"))
    )


# **Asking whether an action is needed is not requesting it.**
#
# "Do I need to reserve the restaurant?" is a policy question and read as a
# request, because `reserve` is a request verb and nothing looked at the frame
# around it. The same shape covers "do I have to book in advance", "is it
# necessary to confirm", "should I cancel" — every one a question about the
# rule rather than an instruction to apply it.
#
# General on purpose: it takes the frame off and lets the rest of the sentence
# decide, so it works for every request verb rather than for the one that
# happened to be noticed. The verb after it survives — "do I need to cancel and
# also cancel it for me" is still a request, because the second clause has no
# frame.
# The frame *and the verb it governs* — removing only the frame leaves the verb
# behind, still matching as a request, which is what the first cut did.
_ASKS_WHETHER_TO = re.compile(
    r"(?xi)"
    r"(?:"
    r"  (?:do|does|did|should|must|would|is|are)\s+"
    r"  (?:i|we|you|it|they|he|she)?\s*"
    r"  (?:need|have|required|necessary|possible|ok|okay|fine)\s+to"
    r"| is\s+it\s+(?:necessary|required|possible)\s+to"
    r")"
    r"\s+\w+"
)


def _strips_the_question_frame(text: str) -> str:
    return _ASKS_WHETHER_TO.sub(" ", text)


def _matches(message: str, *patterns) -> bool:
    return any(p.search(message) for p in patterns)


def _of_one_clause(text: str) -> str:
    """
    Which of the six this message is.

    Defaults to TRANSACTIONAL rather than INFORMATIONAL, and the asymmetry is
    the safety property. A message this cannot read is one the requirement
    detectors should still get their say on: guessing INFORMATIONAL suppresses
    a real business action and loses the hotel a sale silently, while guessing
    TRANSACTIONAL costs a tool call that the detectors were going to make
    anyway. Only a *positively identified* informational question is allowed to
    suppress anything.
    """
    if not text:
        return INFORMATIONAL

    # **Asking *for* a person, not mentioning one.**
    #
    # This was a bare list of person-nouns, so "the manager was lovely" and
    # "the staff were rude" both read as ESCALATION — and ESCALATION owes
    # nothing, so a complaint would have silenced every business action on the
    # turn. A noun deciding a speech act is precisely the failure this module
    # was written to fix, made inside it.
    #
    # `required_actions.asks_for_a_person` is the canonical vocabulary: it is
    # what *forces* the handoff and what the arbiter reads, so deferring means
    # the three cannot disagree about what a request for a human is. Imported
    # inside the function because the dependency runs the other way at module
    # level.
    from app.services.ai import required_actions as _required

    if _required.asks_for_a_person(text):
        return ESCALATION

    amenity = _matches(text, _AMENITY, _AMENITY_HI)
    asks_a_fact = _asks_a_question(text)
    # A request verb inside "do I need to …" is a question about the rule, not
    # an instruction to apply it — see `_ASKS_WHETHER_TO`.
    requests = _matches(_strips_the_question_frame(text),
                        _REQUESTS_SOMETHING, _REQUESTS_SOMETHING_HI)

    # **A factual question about a facility or a policy, with nothing asked
    # for.** Settled here, ahead of every business-noun pattern below, because
    # those patterns cannot tell "is parking free?" from "parking free kar do"
    # and this can: the first has no request in it.
    #
    # All three clauses are load-bearing. Without `amenity` this would swallow
    # "when are you cheapest". Without `asks_a_fact` it would swallow "breakfast
    # add karo". Without `not requests` it would swallow "can you make parking
    # free", which is a haggle wearing a question mark.
    if amenity and asks_a_fact and not requests:
        if _matches(text, _IS_IT_INCLUDED, _IS_IT_INCLUDED_HI) or _matches(
                text, _MINE, _MINE_HI):
            return STATE_LOOKUP
        return INFORMATIONAL

    # **Negotiation before mutation, when a message is both.**
    #
    # "free upgrade mil sakta hai?" matches `upgrade` and came back MUTATION,
    # which routes to the modification path and away from the negotiation rules
    # — so a request for something free was handled as a request for something
    # different. Asking to change a booking and asking for a discount are both
    # real, and when one sentence does both, the discount is the half with a
    # floor and an approval attached.
    #
    # It only reorders messages that match *both*. "meri booking cancel karni
    # hai" and "Can I upgrade to a suite?" contain no discount words and are
    # unaffected.
    # **"Can you give me a free room?" is a haggle; "how many rooms are free?"
    # is availability.**
    #
    # `_FREE_MEANS_VACANT` was vetoing both, because both contain `free` beside
    # `room`. The noun does not separate them and neither does the word order —
    # what separates them is whether the guest is *asking the hotel for*
    # something. A request plus `free` is a request for something free; a
    # question plus `free` is a question about vacancy.
    #
    # So the vacancy reading only applies to a message that asks rather than
    # requests, which is the same distinction the informational branch above is
    # built on rather than a second rule about the word `free`.
    reads_as_vacancy = _FREE_MEANS_VACANT.search(text) and not requests
    if ((_matches(text, _ASKS_FOR_LESS, _ASKS_FOR_LESS_HI)
         and not reads_as_vacancy)
            or _PERCENT_OFF.search(text)):
        return NEGOTIATION
    # The frame comes off here too. "do I have to remove my shoes" and "what is
    # the fee to remove a room" are questions about a rule; "remove one room
    # from my booking" is an instruction. Reading the raw text made all three
    # mutations, which is the same word-decides-the-act failure one branch down.
    if _matches(_strips_the_question_frame(text),
                _CHANGES_A_BOOKING, _CHANGES_A_BOOKING_HI):
        return MUTATION

    wants = _matches(text, _WANTS_SOMETHING, _WANTS_SOMETHING_HI)

    if amenity and _matches(text, _IS_IT_INCLUDED, _IS_IT_INCLUDED_HI):
        return STATE_LOOKUP

    if wants:
        return TRANSACTIONAL

    # A question about my own booking with no acquisitive verb: "kya mera
    # booking confirm hai?" — a read, and only a read.
    if _matches(text, _MINE, _MINE_HI) and asks_a_fact:
        return STATE_LOOKUP

    # The same rule as the branch at the top, and it was missing here. "Can you
    # arrange airport pickup?" reached this line with `requests` already true —
    # `can you` and `arrange` both match — and was returned as a question about
    # the airport rather than a request to book a car.
    if asks_a_fact and amenity and not requests:
        return INFORMATIONAL

    return TRANSACTIONAL


# Clause boundaries. A guest writes one message and asks two things in it more
# often than any other shape in this inbox.
# A full stop is a clause boundary too, and leaving it out cost a real
# message: "Courtyard Standard with breakfast. What's the total?" never
# split, so the amenity noun and the question mark in one half outvoted the
# price question in the other, and a guest asking what their stay costs was
# read as asking about breakfast.
#
# Not a decimal point: `\d\.\d` would split "₹1234.50" into two clauses.
_CLAUSES = re.compile(r"(?<!\d)[.](?!\d)|[,;?!।]|\baur\b|\band\b|\bbhi\b|\bplus\b",
                      re.IGNORECASE)


def category(message: str | None) -> str:
    """
    Which of the six this message is, reading it a clause at a time.

    **A message that asks two things is owed the more consequential answer.**
    "rate kya hai, aur checkout kitne baje hai?" is a price question with a
    clock question attached, and reading it whole gave INFORMATIONAL — the
    amenity noun and the factual mood are both there, and the price question in
    the first half is simply outvoted. Suppressing the quote on that turn is the
    same failure as forcing one on "what time is breakfast", pointed the other
    way: a guest who asked what it costs gets told the checkout time.

    So each clause is categorised on its own and the most consequential wins.
    `_ORDER` is that ranking, and this is what it is for.
    """
    text = (message or "").strip()
    if not text:
        return INFORMATIONAL

    clauses = [c.strip() for c in _CLAUSES.split(text) if c and c.strip()]
    if len(clauses) <= 1:
        return _of_one_clause(text)

    found = {_of_one_clause(c) for c in clauses}
    # A fragment on its own often reads as TRANSACTIONAL through the default —
    # "aur checkout" has no fact question in it. So the whole message is also
    # read, and its verdict counts as one more vote; the ranking then picks.
    found.add(_of_one_clause(text))
    for rank in _ORDER:
        if rank in found:
            return rank
    return TRANSACTIONAL


# ── what each category may owe ──────────────────────────────────────────────
#
# Intent names from `required_actions`, kept as strings so this module does not
# import it — `required_actions` imports *this*, and the dependency runs one
# way. `test_intent_categories` asserts every name here is a real intent, which
# is what a symbolic import would have bought.
_ALL = frozenset({
    "GROUP_BOOKING", "NEGOTIATION", "QUOTE_REQUIRED", "PAYMENT",
    "BOOKING_STATUS", "BOOKING_CANCELLATION", "BOOKING_MODIFICATION",
    "ESCALATION", "ROOM_SELECTION", "DATE_CHANGE", "GUEST_COUNT_CHANGE",
})

ALLOWS: dict[str, frozenset[str]] = {
    # Nothing. This is the whole point of the module.
    INFORMATIONAL: frozenset(),

    # A read of the guest's own booking, and the things that are lookups
    # against it. No pricing, no availability, no hold.
    STATE_LOOKUP: frozenset({"BOOKING_STATUS", "BOOKING_CANCELLATION"}),

    # Everything. A guest who wants a room may need any of it.
    TRANSACTIONAL: _ALL,

    # Changing a booking needs to read it, price the change and check the new
    # dates — but never a group band, which is a new-business concept.
    MUTATION: frozenset({
        "BOOKING_MODIFICATION", "BOOKING_CANCELLATION", "BOOKING_STATUS",
        "DATE_CHANGE", "GUEST_COUNT_CHANGE", "ROOM_SELECTION", "QUOTE_REQUIRED",
        "PAYMENT",
    }),

    # A haggle needs the rules, the published price and the stay it is about.
    NEGOTIATION: frozenset({
        "NEGOTIATION", "QUOTE_REQUIRED", "GROUP_BOOKING", "ROOM_SELECTION",
        "DATE_CHANGE", "GUEST_COUNT_CHANGE", "PAYMENT", "BOOKING_STATUS",
    }),

    # Stopping. Nothing else is owed, and the escalation itself is exempt
    # everywhere in any case.
    ESCALATION: frozenset(),
}

# Never filtered, from any category. A guest asking for a person gets one
# whatever else the sentence was doing — and `required_actions` forces this one,
# so a filter that could remove it would be able to override a forced stop.
ALWAYS_PERMITTED = frozenset({"ESCALATION"})


def permits(message_category: str, intent: str) -> bool:
    """May a message of this category owe this intent?"""
    if intent in ALWAYS_PERMITTED:
        return True
    return intent in ALLOWS.get(message_category, _ALL)
