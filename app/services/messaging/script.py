"""
Which alphabet a reply is written in, and how to put it back.

**The failure this exists for.** A guest asked, in as many words, to be spoken
to in Hinglish. The preference was stored, the prompt says so on every turn, and
the replies came back like this:

    Aapke लिए best rate ₹4,500 rahega.

One word. Not a language switch — the sentence is Hinglish, in Latin, exactly as
asked — with a single Devanagari word dropped into the middle of it. It happens
a few times in a hundred turns and it is unmistakably a machine when it does.

**Why this is code and not another prompt line.** The prompt already says it:
*"Do not reply in Devanagari."* The instruction is there, it is followed on
ninety-odd per cent of turns, and adding a third sentence telling the model the
same thing a third time is the definition of solving a deterministic problem
with a prompt. Which alphabet a string is written in is decidable by looking at
it, and a decidable thing does not belong in a prompt at all.

**What it deliberately does not do.** It never touches a reply to a guest who is
themselves writing Devanagari. Hindi speakers who write in Hindi script get
Hindi script — §10 is explicit about this, and it is the reason the guest's
current message is an argument here rather than just the stored preference. The
preference says which alphabet they asked for; the message says which one they
are using right now, and the second wins.

**Transliteration, not translation.** Nothing here changes a word — `लिए`
becomes `liye`, which is the same Hindi word a Hinglish speaker would have
typed. Digits, currency symbols, Latin runs and punctuation pass through
untouched, which is what makes it safe to run before the reply guardrail: a
₹4,500 that came out of the pricing engine is still character-for-character
₹4,500 afterwards.
"""
import logging
import re

logger = logging.getLogger(__name__)

DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_DEVANAGARI_WORD = re.compile(r"[ऀ-ॿ‌‍]+")


def has_devanagari(text: str | None) -> bool:
    return bool(DEVANAGARI.search(text or ""))


def devanagari_words(text: str | None) -> list[str]:
    """The Devanagari runs in a string, in order. Empty for Latin-only text."""
    return _DEVANAGARI_WORD.findall(text or "")


# ── the transliteration table ────────────────────────────────────────────────
#
# Hindi is written phonetically, so a syllable-by-syllable walk gets most words
# right on its own. What it gets *wrong* is the words that are worn smooth by
# use — `है` is "hai" and not "hai" by any rule the table below knows, `नहीं` is
# "nahi" rather than "nahiin", and those are the words that actually leak.
#
# So the dictionary comes first and the walk is the fallback. The dictionary is
# not a translation table: every entry is the spelling a Hinglish speaker would
# have typed themselves, taken from the transcripts rather than from a
# romanisation standard. "Aapke liye" is what guests write; "āpke liye" is what
# a linguist writes, and one of those two looks like a hotel.

_COMMON = {
    # the sentence frame
    "है": "hai", "हैं": "hain", "हूँ": "hoon", "हूं": "hoon", "हो": "ho",
    "था": "tha", "थी": "thi", "थे": "the", "नहीं": "nahi", "नही": "nahi",
    "ना": "na", "न": "na", "हाँ": "haan", "हां": "haan", "जी": "ji",
    "और": "aur", "या": "ya", "भी": "bhi", "तो": "to", "ही": "hi",
    "का": "ka", "की": "ki", "के": "ke", "को": "ko", "से": "se",
    "में": "mein", "पर": "par", "तक": "tak", "लिए": "liye", "लिये": "liye",
    "साथ": "saath", "बाद": "baad", "पहले": "pehle", "अभी": "abhi",
    # people
    "आप": "aap", "आपका": "aapka", "आपकी": "aapki", "आपके": "aapke",
    "आपको": "aapko", "हम": "hum", "हमें": "humein", "हमारा": "hamara",
    "हमारी": "hamari", "हमारे": "hamare", "मैं": "main", "मुझे": "mujhe",
    "मेरा": "mera", "मेरी": "meri", "मेरे": "mere", "वो": "wo", "ये": "ye",
    "यह": "yah", "वह": "vah", "उनका": "unka", "किसी": "kisi",
    # asking
    "क्या": "kya", "क्यों": "kyun", "कैसे": "kaise", "कैसा": "kaisa",
    "कितना": "kitna", "कितने": "kitne", "कितनी": "kitni", "कब": "kab",
    "कहाँ": "kahan", "कहां": "kahan", "कौन": "kaun", "कौनसा": "kaunsa",
    # doing
    "करो": "karo", "करना": "karna", "करके": "karke", "कर": "kar",
    "करते": "karte", "करता": "karta", "करती": "karti", "करेंगे": "karenge",
    "कीजिए": "kijiye", "कीजिये": "kijiye", "दो": "do", "दीजिए": "dijiye",
    "बताओ": "batao", "बताइए": "bataiye", "देख": "dekh", "देखते": "dekhte",
    "मिलेगा": "milega", "मिलेगी": "milegi", "मिल": "mil", "सकते": "sakte",
    "सकता": "sakta", "सकती": "sakti", "जाएगा": "jayega", "जाएगी": "jayegi",
    "रहेगा": "rahega", "रहेगी": "rahegi", "रहेंगे": "rahenge", "होगा": "hoga",
    "होगी": "hogi", "गया": "gaya", "गई": "gayi", "चाहिए": "chahiye",
    "चाहिये": "chahiye", "बुक": "book", "भेज": "bhej", "भेजा": "bheja",
    "आएगा": "aayega", "लगेगा": "lagega", "पड़ेगा": "padega",
    # hotels
    "कमरा": "kamra", "कमरे": "kamre", "कमरों": "kamron", "होटल": "hotel",
    "रात": "raat", "दिन": "din", "रुपये": "rupaye", "रुपए": "rupaye",
    "पैसा": "paisa", "पैसे": "paise", "छूट": "chhoot", "कम": "kam",
    "ज्यादा": "zyada", "सस्ता": "sasta", "महंगा": "mehnga", "दाम": "daam",
    "कीमत": "keemat", "किराया": "kiraya", "खाना": "khana", "नाश्ता": "nashta",
    "बुकिंग": "booking", "पुष्टि": "pushti", "रद्द": "radd",
    "उपलब्ध": "uplabdh", "खाली": "khali", "लोग": "log", "मेहमान": "mehmaan",
    "तारीख": "tareekh", "समय": "samay", "सुविधा": "suvidha",
    # courtesies
    "नमस्ते": "namaste", "धन्यवाद": "dhanyavaad", "शुक्रिया": "shukriya",
    "कृपया": "kripya", "माफ": "maaf", "स्वागत": "swagat", "ठीक": "theek",
    "बहुत": "bahut", "अच्छा": "accha", "बढ़िया": "badhiya", "ज़रूर": "zaroor",
    "जरूर": "zaroor", "बिल्कुल": "bilkul", "शायद": "shayad",
    "सिर्फ": "sirf", "एक": "ek", "दूसरा": "doosra", "आज": "aaj", "कल": "kal",
}

# Independent vowels.
_VOWELS = {
    "अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo",
    "ऋ": "ri", "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au",
}

# Vowel signs, which replace a consonant's implicit 'a'.
_MATRAS = {
    "ा": "aa", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "ृ": "ri",
    "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ॉ": "o", "ॅ": "e",
    # Deliberately "i" and "u" rather than the "ee"/"oo" a romanisation
    # standard would use. Guests write "kitni", "kaafi", "shuru" — the long
    # marks are technically right and look like a textbook.
}

_CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ल": "l", "व": "v", "श": "sh",
    "ष": "sh", "स": "s", "ह": "h", "ळ": "l",
    # The nukta forms, which guests type as often as the plain ones.
    "क़": "q", "ख़": "kh", "ग़": "g", "ज़": "z", "ड़": "r", "ढ़": "rh",
    "फ़": "f", "य़": "y",
}

_NUKTA = "़"
_VIRAMA = "्"
_ANUSVARA = "ं"
_CHANDRABINDU = "ँ"
_VISARGA = "ः"
_DIGITS = {
    "०": "0", "१": "1", "२": "2", "३": "3", "४": "4",
    "५": "5", "६": "6", "७": "7", "८": "8", "९": "9",
}


def _transliterate_word(word: str) -> str:
    """
    One Devanagari word, walked syllable by syllable.

    The only rule with any subtlety in it is the implicit vowel: a bare
    consonant carries an 'a' unless a vowel sign, a virama or the end of the
    word says otherwise. Dropping the word-final one is what makes `कमरा` come
    out as "kamra" rather than "kamara" — Hindi does it, every Hinglish speaker
    does it, and a transliterator that does not sounds like a robot reading
    Sanskrit.
    """
    out: list[str] = []
    i = 0
    length = len(word)

    while i < length:
        char = word[i]

        # A consonant plus its nukta is one letter with its own sound.
        if i + 1 < length and word[i + 1] == _NUKTA and (char + _NUKTA) in _CONSONANTS:
            char, i = char + _NUKTA, i + 1

        if char in _CONSONANTS:
            out.append(_CONSONANTS[char])
            nxt = word[i + 1] if i + 1 < length else ""

            if nxt in _MATRAS:
                out.append(_MATRAS[nxt])
                i += 2
                continue
            if nxt == _VIRAMA:
                # Half consonant: no vowel at all, the cluster runs on.
                i += 2
                continue
            if nxt in (_ANUSVARA, _CHANDRABINDU):
                out.append("an")
                i += 2
                continue

            # Implicit 'a', except at the end of the word — and except in the
            # middle of one, where Hindi drops it too.
            #
            # The medial rule is one lookahead: a schwa between two consonants
            # disappears when the second consonant carries its own vowel sign.
            # `जानकारी` is "jaankaari" and not "jaanakaari", and the difference
            # between those two is the difference between a Hinglish word and a
            # transliteration exercise. Kept where the lookahead does not apply,
            # which is the conservative direction — an extra 'a' is readable and
            # a missing one can change the word.
            at_end = i + 1 >= length
            medial_schwa_dropped = (
                i + 2 < length
                and word[i + 1] in _CONSONANTS
                and word[i + 2] in _MATRAS
                and out  # never the first syllable: "kamra", not "kmra"
                and len(out) > 1
            )
            if not at_end and not medial_schwa_dropped:
                out.append("a")
            i += 1
            continue

        if char in _VOWELS:
            out.append(_VOWELS[char])
            i += 1
            continue
        if char in _MATRAS:
            # A stray matra with no consonant before it. Malformed, but a
            # dropped character would be worse than an approximate one.
            out.append(_MATRAS[char])
            i += 1
            continue
        if char in (_ANUSVARA, _CHANDRABINDU):
            out.append("n")
            i += 1
            continue
        if char == _VISARGA:
            out.append("h")
            i += 1
            continue
        if char in _DIGITS:
            out.append(_DIGITS[char])
            i += 1
            continue
        if char in ("‌", "‍", _NUKTA):
            i += 1
            continue

        # Devanagari punctuation and anything unmapped.
        out.append({"।": ".", "॥": "."}.get(char, ""))
        i += 1

    return "".join(out)


def romanize(text: str | None) -> str:
    """
    The same sentence with its Devanagari runs written in Latin.

    Everything else is left exactly as it was — Latin words, digits, `₹`,
    punctuation, whitespace. That is what makes this safe to run on a reply
    whose figures came out of the pricing engine: nothing that could be a price
    is inside a Devanagari run, so nothing that could be a price is touched.
    """
    if not text:
        return text or ""

    def replace(match: re.Match) -> str:
        word = match.group(0)
        common = _COMMON.get(word)
        if common:
            return common
        return _transliterate_word(word) or word

    return _DEVANAGARI_WORD.sub(replace, text)


# ── the decision ─────────────────────────────────────────────────────────────

def should_romanize(reply: str | None, *, preference: str | None,
                    guest_message: str | None) -> bool:
    """
    Three conditions, all of them necessary.

      1. The guest asked for Hinglish. Not Hindi, not English — Hinglish is the
         only one of the three that is *defined* by its alphabet, which is why
         it is the only one this can act on.
      2. The reply actually contains Devanagari. Almost always false, which is
         what keeps this off the cost of an ordinary turn.
      3. **The guest is not themselves writing Devanagari.** A guest who
         switches to Hindi script gets Hindi script; the stored preference is
         what they asked for once and the message is what they are doing now,
         and the second wins. Without this the module would be the global ban
         §10 explicitly rules out.
    """
    if preference != "hinglish":
        return False
    if not has_devanagari(reply):
        return False
    return not has_devanagari(guest_message)


def normalise(reply: str | None, *, preference: str | None,
              guest_message: str | None) -> tuple[str | None, list[str]]:
    """
    The reply as the guest should see it, and the words that had to be changed.

    The second element is returned rather than merely logged because it is the
    measurement: `scripts/agent_eval.py` counts these to report a language
    accuracy figure, and a fix whose effect cannot be counted is a fix nobody
    can tell has regressed.
    """
    if not should_romanize(reply, preference=preference, guest_message=guest_message):
        return reply, []

    offending = devanagari_words(reply)
    fixed = romanize(reply)
    logger.info("romanised %d Devanagari word(s) for a Hinglish guest: %s",
                len(offending), offending[:5])
    return fixed, offending
