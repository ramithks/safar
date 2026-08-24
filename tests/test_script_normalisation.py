"""
A guest who asked for Hinglish gets Hinglish, one word at a time.

The audit found *"Aapke लिए best rate ₹4,500 rahega"* — a perfectly romanised
Hinglish sentence with a single Devanagari word dropped into the middle of it.
Not a language switch, not a misunderstood preference: one word, a few times in
a hundred turns.

The prompt already forbids it, on every turn, in as many words. That is the
evidence that a fourth prompt line was not the fix — and the reason this is a
transliteration table instead. Which alphabet a string is written in is
decidable by looking at it, and §24 rule 2 says a deterministic problem does not
get solved with a prompt.

Two things are asserted here with equal weight:

  * that the slip is fixed, and
  * that a guest who is *actually writing Hindi* is left completely alone.

The second is the one that would be a regression if it broke, and §10 names it
explicitly: "Do NOT globally prohibit Devanagari if the guest is actually using
Hindi script."
"""
import pytest

from app.services.messaging import script


# ── the failure, fixed ──────────────────────────────────────────────────────

def test_the_audits_own_sentence():
    fixed, slips = script.normalise(
        "Aapke लिए best rate ₹4,500 rahega.",
        preference="hinglish", guest_message="best rate kya hai",
    )
    assert fixed == "Aapke liye best rate ₹4,500 rahega."
    assert slips == ["लिए"]


@pytest.mark.parametrize("reply,expected", [
    ("Aapke लिए ye rate hai", "Aapke liye ye rate hai"),
    ("Room available है", "Room available hai"),
    ("Haan ji, breakfast शामिल hai", "Haan ji, breakfast shaamil hai"),
    ("Main अभी check karti hoon", "Main abhi check karti hoon"),
    ("Total 3 रात ke liye", "Total 3 raat ke liye"),
    ("Ye rate नहीं badal sakta", "Ye rate nahi badal sakta"),
])
def test_isolated_words_come_back_in_latin(reply, expected):
    fixed, _ = script.normalise(reply, preference="hinglish", guest_message="ok")
    assert fixed == expected


def test_a_whole_sentence_in_the_wrong_script_is_fixed_too():
    """
    The rarer, worse version of the same failure. A guest writing Latin who is
    answered in Devanagari has been ignored, not merely glitched.
    """
    fixed, slips = script.normalise(
        "आपके लिए best rate ₹4,500 रहेगा.",
        preference="hinglish", guest_message="best rate?",
    )
    assert fixed == "aapke liye best rate ₹4,500 rahega."
    assert len(slips) == 3


# ── what must never be touched ──────────────────────────────────────────────

def test_a_guest_writing_hindi_is_answered_in_hindi():
    """
    §10, verbatim: "Do NOT globally prohibit Devanagari if the guest is actually
    using Hindi script." The stored preference is what they asked for once; the
    message is what they are doing now, and the second wins.
    """
    reply = "आपके लिए best rate ₹4,500 रहेगा."
    fixed, slips = script.normalise(
        reply, preference="hinglish", guest_message="मुझे कमरा चाहिए",
    )
    assert fixed == reply
    assert slips == []


@pytest.mark.parametrize("preference", ["hi", "en", None, "", "gu"])
def test_only_a_hinglish_preference_triggers_anything(preference):
    """
    Hinglish is the only one of the three that is *defined* by its alphabet,
    which is why it is the only one this can act on. A Hindi speaker's
    Devanagari is correct and an English speaker never sees any.
    """
    reply = "आपके लिए ye rate hai"
    fixed, slips = script.normalise(reply, preference=preference, guest_message="ok")
    assert fixed == reply
    assert slips == []


def test_a_latin_reply_is_returned_unchanged_and_untouched():
    """The common case, and the one this must cost nothing on."""
    reply = "Aapke liye best rate ₹4,500 rahega, 3 raat ke liye total ₹13,500."
    fixed, slips = script.normalise(reply, preference="hinglish", guest_message="rate?")
    assert fixed is reply
    assert slips == []


@pytest.mark.parametrize("reply", [None, ""])
def test_nothing_happens_to_nothing(reply):
    fixed, slips = script.normalise(reply, preference="hinglish", guest_message="hi")
    assert fixed == reply
    assert slips == []


# ── the safety property ─────────────────────────────────────────────────────

@pytest.mark.parametrize("money", [
    "₹4,500", "₹19,200", "Rs 4500", "INR 12,000", "₹1,20,000", "45%", "TB-4K2M9X",
])
def test_no_figure_survives_transliteration_changed(money):
    """
    **Why this can run before the reply guardrail.** The guardrail's whole job
    is that every rupee figure in a reply came out of a tool. If transliteration
    could alter a digit, it would be laundering a price past the check.

    It cannot, by construction: a Devanagari run contains no ASCII digit, no
    `₹` and no Latin letter, so nothing that could be a price is ever inside the
    span being rewritten. Asserted rather than argued.
    """
    reply = f"Aapke लिए ye rate hai: {money} — theek है?"
    fixed, _ = script.normalise(reply, preference="hinglish", guest_message="ok")
    assert money in fixed


def test_devanagari_digits_are_the_one_exception_and_are_converted():
    """
    `१२००` is a number a model could in principle emit, and leaving it in
    Devanagari would show a guest a price they cannot read. Converted to ASCII
    digits, which is the same number — and the guardrail then checks it exactly
    as it checks any other.
    """
    fixed, _ = script.normalise("Rate १२०० hai", preference="hinglish",
                                guest_message="rate?")
    assert fixed == "Rate 1200 hai"


def test_latin_and_punctuation_pass_through():
    fixed, _ = script.normalise(
        "Deluxe Balcony (2 adults) — ₹4,500/night. Book करें?",
        preference="hinglish", guest_message="ok",
    )
    assert fixed.startswith("Deluxe Balcony (2 adults) — ₹4,500/night. Book ")
    assert "करें" not in fixed


# ── the table itself ────────────────────────────────────────────────────────

@pytest.mark.parametrize("word,expected", [
    # The words that actually leak, from the transcripts.
    ("है", "hai"), ("हैं", "hain"), ("नहीं", "nahi"), ("लिए", "liye"),
    ("आपके", "aapke"), ("मुझे", "mujhe"), ("कमरे", "kamre"), ("चाहिए", "chahiye"),
    ("रहेगा", "rahega"), ("मिलेगा", "milega"), ("कितना", "kitna"),
    ("धन्यवाद", "dhanyavaad"), ("नमस्ते", "namaste"),
])
def test_the_words_guests_actually_see(word, expected):
    assert script.romanize(word) == expected


@pytest.mark.parametrize("word,expected", [
    # Not in the dictionary — the syllable walk has to carry these.
    ("जानकारी", "jaankaari"),
    ("शामिल", "shaamil"),
    ("सुबह", "subah"),
    ("उपलब्ध", "uplabdh"),
])
def test_words_the_dictionary_does_not_know_are_still_readable(word, expected):
    """
    The fallback is a syllable walk with one piece of subtlety in it — Hindi
    drops the schwa between two consonants when the second carries its own
    vowel. Without that rule `जानकारी` comes out "jaanakaaree", which is a
    transliteration exercise rather than a word anybody types.
    """
    assert script.romanize(word) == expected


def test_a_word_it_cannot_read_is_kept_rather_than_dropped():
    """
    A dropped word is a sentence with a hole in it, which is worse than a word
    spelled oddly. Nothing here may return empty.
    """
    assert script.romanize("ॐ") != ""


def test_detection_is_the_gate_and_it_is_cheap():
    assert script.has_devanagari("Aapke लिए") is True
    assert script.has_devanagari("Aapke liye") is False
    assert script.has_devanagari("") is False
    assert script.has_devanagari(None) is False
    assert script.devanagari_words("a लिए b है") == ["लिए", "है"]


# ── the bug the live replay found ───────────────────────────────────────────
#
# The romanisation above is the *last* line of defence and it only fires when
# the turn knows the guest wants Hinglish. What the live replay exposed is a
# turn that had stopped knowing.

class TestThePreferenceThatGotOverwritten:
    """
    A guest asked for Hinglish, then wrote four words, and got Devanagari.

    The message was:

        Breakfast included h isme?

    Four words, one Hinglish marker ("h"), and `detect` returned `en` —
    because `en` was the fallback for "not obviously Hinglish". A returned
    language is a *claim*, and `pipeline` stores a claim, so the stored
    preference flipped to English on a sentence that was Hinglish. The prompt
    then carried no Hinglish instruction, and the reply came back in Devanagari:
    neither the language stored, nor the one just claimed, nor the one the guest
    was writing in.

    Two things were wrong and both are fixed here rather than in the prompt:

      * `isme` was missing from the vocabulary. It is a postposition — exactly
        the class of function word the list is built from.
      * `en` was being claimed without evidence. **Hinglish is mostly English
        words**; the absence of Hindi grammar is not the presence of English.

    Found by `scripts/replay_e2e.py`, which is the only thing in the suite that
    runs a real conversation through the real model. No unit test would have
    produced that sentence.
    """

    def test_the_sentence_that_broke_it(self):
        from app.services.messaging import language

        assert language.detect("Breakfast included h isme?") == language.HINGLISH

    @pytest.mark.parametrize("message", [
        "Breakfast included h isme?",
        "Room me AC hai?",
        "iska rate kya hoga",
        "usme breakfast included hai",
    ])
    def test_a_hinglish_sentence_is_never_claimed_as_english(self, message):
        from app.services.messaging import language

        assert language.detect(message) != language.EN, (
            f"{message!r} claimed as English would overwrite a stored Hinglish "
            f"preference, which is how a guest who asked for Hinglish gets "
            f"answered in Devanagari"
        )

    @pytest.mark.parametrize("message", [
        "Could you send me the cancellation policy please",
        "Do you have photos of the rooms",
        "What is the price for two nights",
        "please send me the payment link",
    ])
    def test_real_english_is_still_recognised(self, message):
        """
        The other direction. A detector that never claims English would pin
        every guest to whatever they said first, and a guest who switches
        language should be followed.

        `please`, `sir` and `madam` used to be in the Hinglish vocabulary and
        are not markers — they are loan words, shared by both languages, which
        is the property that disqualifies them under the list's own rule.
        """
        from app.services.messaging import language

        assert language.detect(message) == language.EN

    def test_an_ambiguous_sentence_leaves_the_preference_alone(self):
        """
        None is a real answer and the safe one: it means "nothing changed",
        and `pipeline` then keeps whatever the guest asked for.
        """
        from app.services.messaging import language

        assert language.detect("thoda kam karo na") is None
        assert language.of_conversation(
            {"guest_profile": {"language": "hinglish"}}
        ) == "hinglish"

    def test_no_marker_in_the_vocabulary_is_an_ordinary_english_word(self):
        """
        The rule the list states about itself, enforced. A marker that appears
        in ordinary English prose makes the detector unsure of English
        sentences, which is what "please" was doing.
        """
        from app.services.messaging import language

        english = {
            "please", "sir", "madam", "the", "and", "or", "to", "in", "on",
            "at", "is", "are", "was", "were", "have", "has", "do", "does",
            "can", "will", "would", "me", "my", "you", "your", "we", "our",
            "a", "an", "of", "for", "with", "from", "by", "it", "this", "that",
        }
        overlap = language._HINGLISH_MARKERS & english
        assert not overlap, (
            f"these are English words being used as Hindi markers: "
            f"{sorted(overlap)} — each one makes an English sentence look "
            f"ambiguous and stops the detector following a guest who switched"
        )
