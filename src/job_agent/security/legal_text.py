"""Deterministic detection of legal / eligibility questions and risky drafted claims.

Used wherever a legal declaration or an unverified claim could slip through:

* ``is_legal_question`` -- application form fields (``applications.field_mapping``)
  and the local-LLM answer drafter (``llm.generation.draft_answer``);
* ``mentions_work_authorization`` / ``mentions_legal_matter`` -- statements in
  drafted replies and follow-ups ("I am authorized to work for any employer");
* ``draft_claim_problems`` -- drafted text that asserts numbers (also with a
  changed qualifier or unit), tools/skills, leadership, credentials, degrees,
  employers or job titles that do not trace to the verified profile.

The rules are conservative on purpose: a false positive only sends a question
to you for review (or replaces an LLM draft by the safe template); a false
negative could let a work-authorization, sponsorship, citizenship, export-control,
clearance or criminal-history declaration be answered or asserted without you.

How text is read: ``security.textnorm.normalize_text`` first (full-width and
look-alike letters, invisible characters, circled numerals), then lowercase
ASCII words. Every check runs on several readings of the same text -- as
written, with invisible characters read as spaces, with letter-spaced words
joined ("s p o n s o r"), and with near-misses of the key words corrected
("sponsership", "citzen", "clearence", "authorised", "wrok"). Common Spanish,
Portuguese, French, Italian, German and Dutch wordings are covered, and text in
a script these patterns cannot read counts as legal.

No model calls: plain regular expressions. ``draft_claim_problems`` reuses the
résumé validator's number / credential / name rules (``resume.validation``,
imported lazily inside the function); nothing else imports a feature module.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from functools import lru_cache
from typing import Any

from job_agent.security.textnorm import has_non_ascii_numerals, normalize_text

# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #
#: Latin letters that Unicode decomposition does not turn into ASCII.
_LATIN_FOLD = str.maketrans({
    "ß": "ss", "ẞ": "SS", "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ł": "l", "Ł": "L",
    "đ": "d", "Đ": "D", "þ": "th", "Þ": "Th", "ð": "d", "Ð": "D", "ħ": "h", "Ħ": "H", "ŧ": "t", "Ŧ": "T",
})


def _folded(text: str | None) -> str:
    return unicodedata.normalize("NFKD", normalize_text(text).translate(_LATIN_FOLD))


def normalize(text: str | None) -> str:
    """Lowercase ASCII words separated by one space ("U.S." -> "u s", "H-1B" -> "h 1b", "Ｖｉｓａ" -> "visa")."""
    ascii_text = _folded(text).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_text.lower()).split())


def has_unreadable_letters(text: str | None) -> bool:
    """Letters that survive neither look-alike mapping nor accent stripping (e.g. non-Latin scripts).

    Such text cannot be checked by the patterns here, so callers treat it as
    possibly legal (review) rather than assuming it is harmless.
    """
    return any(ord(ch) > 127 and unicodedata.category(ch).startswith("L") for ch in _folded(text))


def _invisible_as_spaces(text: str) -> str:
    return "".join(" " if (unicodedata.category(ch) == "Cf" or not normalize_text(ch)) and not ch.isspace() else ch
                   for ch in text)


# --------------------------------------------------------------------------- #
# Near-miss spellings of the key words
# --------------------------------------------------------------------------- #
#: (stem, replacement, max edit distance, whole word only). A word whose start is within the
#: distance of the stem (same first letter, at most one letter shorter) is read as the replacement.
_FUZZY_STEMS: tuple[tuple[str, str, int, bool], ...] = (
    ("sponsor", "sponsor", 2, False),
    ("citizen", "citizen", 2, False),
    ("clearance", "clearance", 2, False),
    ("immigra", "immigration", 2, False),
    ("incarcerat", "incarcerated", 2, False),
    ("authoriz", "authorized", 1, False),
    ("residen", "resident", 1, False),
    ("convict", "convicted", 1, False),
    ("felon", "felony", 1, False),
    ("eligib", "eligible", 1, False),
    ("passport", "passport", 1, False),
    ("petition", "petition", 1, False),
    ("nationalit", "nationality", 1, False),
    ("visa", "visa", 1, True),
    ("crime", "crime", 1, True),
    ("legally", "legally", 1, True),
    ("permitted", "permitted", 1, True),
    ("work", "work", 1, True),
    ("working", "working", 1, True),
)
#: Ordinary words that happen to be close to a stem above.
_FUZZY_EXCLUDE = frozenset("""
via vis vise viva vita vida vista visit vias word words wore worn worm worms wok wonk wort wording worming
crimp chime creme sensor sensors clarence authority authorities authoritative nationalist nationalism
nationalists cities feline felines
""".split())
_FUZZY_EXCLUDE_PREFIXES = ("authorit", "sensor")


def _osa(a: str, b: str) -> int:
    """Optimal-string-alignment edit distance (insertions, deletions, substitutions, adjacent swaps)."""
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[len(b)]


@lru_cache(maxsize=4096)
def _fuzzy_word(word: str, split_at: int = 0) -> str | None:
    """The key word ``word`` is (a near-miss of), or None.

    ``split_at`` > 0: ``word`` was joined from two tokens and the first one
    (``word[:split_at]``) must be too short to be the key word by itself.
    """
    if len(word) < 3 or not word.isalpha() or word in _FUZZY_EXCLUDE or word.startswith(_FUZZY_EXCLUDE_PREFIXES):
        return None
    for stem, replacement, max_dist, whole in _FUZZY_STEMS:
        if word[0] != stem[0] or len(word) < len(stem) - 1 or (split_at and split_at >= len(stem) - 1):
            continue
        if whole:
            if abs(len(word) - len(stem)) <= max_dist and _osa(word, stem) <= max_dist:
                return replacement
            continue
        for k in range(len(stem) - 1, min(len(word), len(stem) + max_dist) + 1):
            if _osa(word[:k], stem) <= max_dist:
                return replacement
    return None


def _corrected(norm: str) -> str:
    """``norm`` with near-misses of key words replaced ("sponser ship" -> "sponsor")."""
    tokens = norm.split()
    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if len(tok) >= 2 and len(nxt) >= 2 and not _fuzzy_word(tok):
            joined = _fuzzy_word(tok + nxt, len(tok))  # a key word split in two ("spon sorship", "citi zen")
            if joined:
                out.append(joined)
                i += 2
                continue
        out.append(_fuzzy_word(tok) or tok)
        i += 1
    return " ".join(out)


_LETTER_RUN = re.compile(r"\b(?:[a-z0-9] ){2,}[a-z0-9]\b")


def _letters_joined(norm: str) -> str:
    """Letter-spaced words joined ("s p o n s o r s h i p" -> "sponsorship")."""
    return _LETTER_RUN.sub(lambda m: m.group(0).replace(" ", ""), norm)


@lru_cache(maxsize=2048)
def _variants_cached(text: str) -> frozenset[str]:
    base = {normalize(text), normalize(_invisible_as_spaces(text))}
    out = set(base)
    for v in base:
        joined = _letters_joined(v)
        out.update({joined, _corrected(v), _corrected(joined)})
    out.discard("")
    return frozenset(out)


def _variants(text: str | None) -> frozenset[str]:
    """Readings of ``text`` that every pattern is checked against (see the module docstring)."""
    return _variants_cached(text or "")


def _compile(parts: Iterable[str]) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in parts))


# --------------------------------------------------------------------------- #
# Legal / eligibility topics (applied to normalized text)
# --------------------------------------------------------------------------- #
_US = r"(?:u s(?: a)?|usa|us|united states(?: of america)?|america)"
_VISA_TYPES = r"\b(?:[fjm] ?1|h ?1 ?b ?1?|h ?1|h ?2 ?[ab]|h ?4|l ?1 ?[ab]?|o ?1 ?[ab]?|e ?3|tn (?:visa|status|holder|classification))\b"
_WORK_WORDS = r"(?:trabaj\w*|trabalh\w*|travail\w*|lavor\w*|arbeit\w*|werk\w*)"

#: Work authorization, sponsorship, immigration and clearance (shared by questions and statements).
_WORK_AUTH_CORE = (
    r"\b(?:authori[sz]\w*|eligib\w*|permit\w*|permission|allowed|entitled|legally|lawfully|right)"
    r"(?: \w+){0,3} (?:work|working|employment|employed|employ)\b",
    r"\bauthori[sz]\w*\b",
    r"\bwork auth\w*\b", r"\bemployment auth\w*\b",
    r"\bwork (?:permit|permits|visa|visas|rights?|status|eligibility|pass)\b",
    r"\bwork\b(?: \w+){0,8} without (?:any )?(?:restriction|restrictions|limitation|limitations|sponsorship|sponsor|"
    r"a visa|visa|visas|support)\b",
    r"\bunrestricted\b", r"\bwithout (?:any )?(?:restriction|restrictions)\b",
    r"\brestrictions? (?:on|to) (?:your |my )?(?:ability to )?(?:work|employment)\b",
    r"\bsponsor\w*\b",
    r"\bimmigra\w*\b", r"\bnonimmigrant\b", r"\bvisas?\b",
    r"\bpetition\w*\b", r"\bemployment based\b", r"\blabor certification\b", r"\bperm\b",
    _VISA_TYPES,
    r"\b(?:stem )?opt\b(?! (?:in|out)\b)", r"\bcpt\b", r"\b(?:optional|curricular) practical training\b",
    r"\bsevis\b", r"\bi ?20\b", r"\bds ?2019\b",
    r"\bead\b", r"\bgreen card\b", r"\bpermanent residen\w*\b", r"\blawful permanent\b", r"\blpr\b",
    r"\bresiden\w*\b",
    r"\bresident aliens?\b", r"\baliens?\b", r"\basyl\w*\b", r"\brefugees?\b", r"\bdaca\b", r"\bdeferred action\b",
    r"\btemporary protected status\b", r"\btps\b",
    r"\bcitizen\w*\b", r"\bnationality\b", rf"\b(?:{_US}|american|dual|foreign) nationals?\b", r"\bnational of\b",
    r"\bprotected individual\b", r"\b1324b\b", r"\b8 u s c\b",
    r"\bchange (?:of|in) (?:\w+ )?status\b", r"\bstatus (?:change|transfer)\b",
    r"\b(?:visa|petition|status|h ?1 ?b) transfer\b", r"\btransfer (?:of )?(?:your |my )?(?:visa|status|petition|h ?1 ?b)\b",
    r"\bcap (?:exempt|gap|subject)\b",
    r"\bi ?9\b", r"\bemployment eligibility\b", r"\be ?verify\b",
    r"\bproof of (?:identity|eligibility|citizenship|status|work|employment)\b",
    # "What is your current status in the U.S.?", "student status"
    rf"\bstatus\b(?: \w+){{0,3}} (?:in|within|inside) (?:the |this )?(?:{_US}|country)\b",
    rf"\b(?:{_US}|student|legal|residency|residence|resident|visa|immigration|citizenship|alien|nonimmigrant|"
    r"f 1|j 1|h 1b|opt|cpt|tn|dependent|h 4) status\b",
    r"\bright to (?:remain|stay|live|reside|be employed)\b",
    # someone files paperwork with the government for you
    r"\b(?:file|files|filed|filing|submit|submits|submitted|submitting|sign|signs|signing)\b(?: \w+){0,6} "
    r"(?:on|in) (?:my|your|their|our|his|her) behalf\b",
    r"\b(?:file|files|filed|filing|paperwork|forms?)\b(?: \w+){0,6} (?:with|to|for) (?:the )?(?:government|uscis|"
    r"immigration|authorities|department of labor|dol|state department)\b",
    r"\b(?:government|uscis|immigration) (?:paperwork|filings?|forms?)\b",
    # security clearance held / level
    r"\bcleared\b(?: \w+){0,4} (?:ts|sci|secret|top secret|clearance|level|public trust|polygraph|q|l|dod|doe|"
    r"government|federal|security)\b",
    r"\b(?:ts|sci|q|l|secret|top secret) (?:level|clearance|cleared|eligib\w*)\b",
    r"\b(?:hold|holds|have|has|possess|possesses|maintain|maintains|carry|carries|active|current|valid)"
    r"(?: \w+){0,2} (?:ts|sci|ts sci|secret|top secret|clearance)\b",
    # Spanish, Portuguese, French, Italian, German and Dutch wordings
    r"\bautori[sz]\w*\b", r"\bautoriz\w*\b", r"\bpatrocin\w*\b", r"\bparrain\w*\b", r"\bhabilitad[oa]s?\b",
    r"\bhabilite\w*\b", r"\blegal(?:mente|ement)\b",
    rf"\bpermis[oa]?s?\b(?: \w+){{0,3}} (?:para |de |pour |di |zu )?(?:{_WORK_WORDS}|residencia|residir|soggiorno|"
    r"sejour)\b",
    r"\bpermiso de trabajo\b", r"\bpermis de (?:travail|sejour)\b", r"\bpermess[oi] di (?:lavoro|soggiorno)\b",
    r"\bpermissao\b", r"\btitre de sejour\b", r"\bsoggiorno\b",
    rf"\b(?:droit|derecho|direito|diritto|recht)\b(?: \w+){{0,2}} (?:de |a |au |di |zu |zum )?{_WORK_WORDS}\b",
    r"\bvisados?\b", r"\bvistos?\b", r"\bvisum\b", r"\bvisums\b", r"\bvisas?\b",
    r"\bciudadan\w*\b", r"\bcidada\w*\b", r"\bcitoyen\w*\b", r"\bcittadin\w*\b", r"\bstaatsb\w*\b",
    r"\bstaatsangeh\w*\b", r"\bnacionalidad\w*\b", r"\bnationalite\b", r"\bnazionalita\b", r"\bnationaliteit\b",
    r"\bresidente permanente\b", r"\btarjeta verde\b", r"\bcartao verde\b", r"\bmigratori\w*\b",
    r"\bmigracion\b", r"\bmigracao\b", r"\bextranjer\w*\b", r"\bestrangeir\w*\b",
    r"\barbeitsgenehmigung\w*\b", r"\barbeitserlaubnis\w*\b", r"\barbeitsberechtigung\w*\b", r"\baufenthalt\w*\b",
    r"\bniederlassungserlaubnis\b", r"\bblaue karte\b", r"\bwerkvergunning\w*\b", r"\bverblijfsvergunning\w*\b",
    r"\b(?:sin|sem|sans|senza|ohne|zonder) (?:\w+ ){0,2}(?:restric\w*|restriz\w*|einschrank\w*|beschrank\w*|"
    r"beperking\w*)\b",
)

_LEGAL_QUESTION_EXTRA = (
    r"\beligib\w*\b",
    r"\b(?:legally|lawfully|lawful)\b",
    r"\blegal\b(?! (?:first |last |middle |full |given |family )?(?:name|names|surname)\b)",
    r"\bable to (?:legally |lawfully )?work\b(?=(?: \w+){0,6} (?:in|within|for|without|anywhere|legally|lawfully|"
    r"permanently)\b)",
    r"\b(?:can|could|may) you (?:legally |lawfully )?(?:work|be employed)(?: \w+){0,2} (?:in|within|for)\b",
    rf"\b(?:work|working|employed|employment)(?: \w+){{0,2}} (?:in|within) (?:the )?{_US}\b",
    r"\bpassports?\b",
    # security clearance
    r"\bclearances?\b", r"\bts ?sci\b", r"\bsci\b", r"\b(?:top )?secret\b", r"\bpublic trust\b", r"\bpolygraph\b",
    r"\bsf ?86\b", r"\bsuitability determination\b", r"\bcleared\b", r"\bts level\b",
    # export control
    r"\bexport\w*\b", r"\bitar\b", r"\bear\b", rf"\b{_US} persons?\b", r"\bdeemed exports?\b", r"\bofac\b",
    r"\bsanction\w*\b", r"\bembargo\w*\b", r"\bforeign (?:national|nationals|person|persons|government|principal)\b",
    # criminal history / background checks
    r"\bcrim\w*\b", r"\bconvict\w*\b", r"\bfelon\w*\b", r"\bmisdemean\w*\b", r"\barrest\w*\b", r"\bguilty\b",
    r"\bno contest\b", r"\bnolo contendere\b", r"\bincarcerat\w*\b", r"\bimprison\w*\b", r"\bprisons?\b",
    r"\bjail\w*\b", r"\bprobation\b", r"\bparoled?\b", r"\bindict\w*\b", r"\bsentenced\b",
    r"\bpending (?:\w+ )?charges?\b", r"\bcharges? (?:\w+ )?(?:pending|against)\b",
    r"\bbackground (?:check|checks|investigation|investigations|screening|screenings|verification)\b",
    r"\bcharged with\b", r"\boffen[cs]es?\b", r"\bdrug (?:test|tests|testing|screen|screening)\b", r"\bdebar\w*\b",
    r"\bexcluded from\b",
    r"\bdelitos?\b", r"\bantecedentes\b", r"\bcondenad\w*\b", r"\bcondamn\w*\b", r"\bcondann\w*\b",
    r"\bverurteil\w*\b", r"\bvorstraf\w*\b", r"\bstrafregist\w*\b", r"\bfuhrungszeugnis\w*\b", r"\bcasier judiciaire\b",
    r"\breati\b", r"\bstrafblad\b",
    # restrictive covenants, relatives / conflicts of interest
    r"\bnon ?compet\w*\b", r"\bnoncompet\w*\b", r"\bnon ?solicit\w*\b", r"\bnonsolicit\w*\b",
    r"\brestrictive covenants?\b", r"\bbound by\b",
    r"\bagreements?\b(?: \w+){0,8} (?:prevent|prevents|restrict|restricts|limit|limits|prohibit|prohibits|bar|"
    r"bars)\b",
    r"\bsubject to (?:an|any|a) (?:\w+ ){0,2}agreements?\b",
    r"\bprevent (?:you|me) from (?:working|being employed|accepting|performing|joining)\b",
    r"\brelatives?\b", r"\bfamily members?\b", r"\brelated to (?:any|an|a|someone|anyone)\b",
    r"\b(?:anyone|anybody|someone|somebody|people|person|persons|individuals?)(?: \w+){0,3} related to\b",
    r"\brelated to\b(?: \w+){0,3} (?:employed|working|works|employees?)\b",
    r"\bconflicts? of interest\b",
)

_WORK_AUTH_STATEMENT_EXTRA = (
    r"\b(?:able|free|cleared|qualified)(?: \w+){0,2} to (?:legally |lawfully )?work\b(?=(?: \w+){0,6} "
    r"(?:in|within|for|without|anywhere|legally|lawfully|permanently)\b)",
    r"\b(?:legally|lawfully)\b",
    r"\b(?:employer|visa|immigration|sponsorship|employment|work) support\b",
    r"\bsupport (?:from|of|by) (?:an|any|the|my|a|your) (?:employer|company)\b",
    rf"\bany (?:{_US} )?employers?\b",
    rf"\b(?:work|working|employed|employment)(?: \w+){{0,3}} (?:in|within|for) (?:the )?{_US}\b",
    rf"\b{_US} persons?\b", r"\bclearances?\b", r"\bitar\b", r"\bexport control\w*\b", r"\bpaperwork\b",
)

LEGAL_QUESTION_RE = _compile(_WORK_AUTH_CORE + _LEGAL_QUESTION_EXTRA)
WORK_AUTH_STATEMENT_RE = _compile(_WORK_AUTH_CORE + _WORK_AUTH_STATEMENT_EXTRA)

#: Compensation, EEO and identity questions: not legal declarations, but never drafted by the LLM either.
SENSITIVE_QUESTION_RE = re.compile(
    r"\b(?:salary|salaries|compensation|pay|paid|wages?|remuneration|bonus|equity|stipend|hourly rate|rate of pay|"
    r"desired (?:annual )?base|base pay|total cash|ote|disabilit\w*|disabled|veterans?|military|armed forces|gender|"
    r"sex|race|racial|ethnic\w*|hispanic|latin[oax]|sexual orientation|lgbt\w*|transgender|pronouns?|"
    r"date of birth|birth ?date|dob|age|religio\w*|marital|pregnan\w*|social security|ssn|national origin|"
    r"passport|driver s licen[cs]e|salario|sueldo|gehalt|salaire|stipendio|remunera\w*)\b"
)


def _legal_topic(text: str | None) -> bool:
    return any(LEGAL_QUESTION_RE.search(v) for v in _variants(text))


def is_legal_question(text: str | None) -> bool:
    """Does this question (label, field name, help text) concern a legal / eligibility declaration?

    Covers work authorization and eligibility, sponsorship and visas (F-1, J-1,
    H-1B, TN, O-1, OPT/CPT/STEM OPT, EAD, petitions, change of status),
    immigration status, citizenship / nationality / residence, protected-individual
    status, passports, security clearances, export control (ITAR/EAR, U.S.
    person), criminal history and background checks, non-compete agreements and
    relatives / conflicts of interest -- also with typos and in common European
    languages. Text in a script these patterns cannot read counts as legal.
    """
    if not (text or "").strip():
        return False
    if has_unreadable_letters(text):
        return True
    return _legal_topic(text)


def is_sensitive_question(text: str | None) -> bool:
    """Compensation / EEO / identity questions (answered by you, never drafted)."""
    return any(SENSITIVE_QUESTION_RE.search(v) for v in _variants(text))


def is_never_drafted(text: str | None) -> bool:
    """Questions the local LLM must never draft an answer for (legal, sensitive or unreadable)."""
    return is_legal_question(text) or is_sensitive_question(text)


def mentions_work_authorization(text: str | None) -> bool:
    """Does this text *state* something about work authorization, sponsorship, immigration status or clearance?

    E.g. "I am eligible to work for any employer", "I will not need any employer
    support", "authorized to work in the United States", "without sponsorship",
    "I am a U.S. citzen", "Ich brauche kein Visum", "I'm cleared at the TS level".
    """
    if not (text or "").strip():
        return False
    return any(WORK_AUTH_STATEMENT_RE.search(v) for v in _variants(text))


def mentions_legal_matter(text: str | None) -> bool:
    """Work-authorization statement or any legal topic (clearance, export control, criminal history, ...)."""
    return mentions_work_authorization(text) or is_legal_question(text)


_YES_NO_WORDS = frozenset({"yes", "no", "nope", "yep", "yeah", "si", "oui", "non", "ja", "nein", "sim", "nao"})
_YES_NO_START = re.compile(r"^\W*(?:yes|no|nope|yep|yeah|si|oui|non|ja|nein|sim|nao)\s*(?:[,.!;:]|\s[-–—]|[–—]|$)")


def is_yes_no_answer(text: str | None) -> bool:
    """Does a drafted answer start as a yes/no reply ("No, not now or in the future.", "Yes I can")?

    An open-ended question never needs one; a draft that answers yes/no is
    answering a (possibly legal) yes/no question and is dropped.
    """
    words = normalize(text).split()
    folded = _folded(text).encode("ascii", "ignore").decode().casefold()
    return bool(_YES_NO_START.match(folded) or (words and words[0] in _YES_NO_WORDS and len(words) <= 4))


# --------------------------------------------------------------------------- #
# Numbers (digits and number words)
# --------------------------------------------------------------------------- #
_DIGITS_RE = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)*")
_UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
          "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
         "ninety": 90}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1_000_000}
_TIME_UNITS = frozenset({"year", "years", "yr", "yrs", "month", "months", "week", "weeks", "decade", "decades"})
_VAGUE = frozenset({"several", "many", "multiple", "numerous", "few", "countless", "dozens", "hundreds",
                    "thousands"})
#: Multiplier words ("cut the effort in half", "doubled throughput", "fivefold"): quantities a fact must state.
_MULTIPLIER_WORDS = re.compile(r"\b(half|halved|halving|double|doubled|doubling|triple|tripled|tripling|quadruple|"
                               r"quadrupled|twice|thrice|[a-z0-9]+fold)\b(?! check)")


def _canon(number: str) -> str:
    value = number.replace(",", "")
    if "." in value:
        return value.rstrip("0").rstrip(".") or "0"
    return str(int(value)) if value.isdigit() else value


def numbers_mentioned(text: str | None) -> set[str]:
    """Quantities in ``text`` as canonical digit strings ("three" -> "3", "1,200" -> "1200", "30%" -> "30").

    Read after ``normalize_text``, so circled, full-width or superscript numerals
    count too ("⑳" -> "20"). "one" only counts before a time unit ("one year"), so
    "one of the teams" is not a number.
    """
    found = {_canon(m.group(0)) for m in _DIGITS_RE.finditer(normalize_text(text))}
    tokens = normalize(text).split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        value: int | None = None
        if tok in _TENS:
            value = _TENS[tok]
            if 0 < _UNITS.get(nxt, 0) < 10:
                value += _UNITS[nxt]
                i += 1
        elif tok in _UNITS and (tok != "one" or nxt in _TIME_UNITS):
            value = _UNITS[tok]
        elif tok == "dozen":
            value = 12
        if value is not None:
            after = tokens[i + 1] if i + 1 < len(tokens) else ""
            if after in _SCALES:
                value *= _SCALES[after]
                i += 1
            found.add(str(value))
        i += 1
    return found


def vague_quantities(text: str | None) -> set[str]:
    """Unquantified duration claims such as "several years", "many months" or "years of"."""
    tokens = normalize(text).split()
    found = {f"{a} {b}" for a, b in zip(tokens, tokens[1:]) if a in _VAGUE and b in _TIME_UNITS}
    found |= {f"{a} of" for a, b in zip(tokens, tokens[1:]) if a in {"years", "months", "decades"} and b == "of"}
    return found

# --------------------------------------------------------------------------- #
# Experience claims in drafted text
# --------------------------------------------------------------------------- #
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;:])\s+|\n+")
_FIRST_PERSON = frozenset({"i", "my", "mine", "myself", "we", "our", "ours", "ourselves"})
#: Words that make a first-person sentence an experience claim (verb forms, so that job
#: titles such as "Data Analyst" or "Engineering Manager" do not count by themselves).
_CLAIM_CUE = re.compile(
    r"\b(?:experience[ds]?|years?|yrs?|background|expertise|expert|proficien\w*|skills?|skilled|knowledge|know|"
    r"familiar\w*|fluent|strengths?|speciali[sz](?:e|ed|es|ing)|certified|certifications?|trained|bring|hands on|"
    r"work|worked|working|use|used|using|build|built|building|develop(?:ed|ing|s)?|design(?:ed|ing)|"
    r"implement(?:ed|ing)?|deliver(?:ed|ing)?|creat(?:e|ed|ing)|launch(?:ed|ing)?|deploy(?:ed|ing)?|"
    r"automat(?:e|ed|ing)|analy[sz](?:e|ed|es|ing)|improv(?:e|ed|ing)|increas(?:e|ed|ing)|reduc(?:e|ed|ing)|"
    r"sav(?:e|ed|ing)|grew|grow(?:n|ing)?|optimi[sz](?:e|ed|ing)|migrat(?:e|ed|ing)|convert(?:ed|ing)|"
    r"architect(?:ed|ing)|engineer(?:ed|ing)|achiev(?:e|ed|ing)|complet(?:e|ed|ing)|earn(?:ed|ing)|wrote|written|"
    r"maintain(?:ed|ing)?|support(?:ed|ing)|ran|run|running|own|owned|ownership|led|lead|leads|leading|"
    r"manag(?:e|ed|es|ing)|supervis(?:e|ed|ing)|head(?:ed|ing)|direct(?:ed|ing)|direct reports?|"
    r"overs(?:aw|ee|eeing)|spearhead(?:ed|ing)|mentor(?:ed|ing)|coach(?:ed|ing)|found(?:ed)|chair(?:ed|ing)|"
    r"responsib(?:le|ility|ilities)|in charge|cut|cutting|slashed|boosted|accelerated|streamlined|modernized|"
    r"halved|doubled|tripled)\b"
)
#: Sentences that start with one of these are claims even without "I" ("Led a team of five").
_CLAIM_START = re.compile(
    r"^(?:led|managed|supervised|headed|directed|oversaw|spearheaded|owned|mentored|coached|founded|chaired|"
    r"built|developed|designed|implemented|delivered|created|launched|deployed|automated|analy[sz]ed|improved|"
    r"increased|reduced|saved|grew|optimi[sz]ed|migrated|converted|architected|engineered|achieved|completed|"
    r"earned|wrote|maintained|supported|ran|worked|used|have|has|had|experienced|skilled|proficient|expert|"
    r"responsible)\b"
)

#: (claim pattern, stem a verified fact must contain to support it)
_LEADERSHIP: tuple[tuple[re.Pattern[str], re.Pattern[str]], ...] = tuple(
    (re.compile(claim), re.compile(stem)) for claim, stem in (
        (r"\bled\b|\blead(?:s|ing)? (?:a|an|the|my|our|this|that|teams?|projects?|groups?|people|staff|analysts?|"
         r"engineers?|developers?|efforts?|initiatives?|workstreams?)\b|\b(?:team|tech|technical|project|group) lead\b",
         r"\b(?:led|lead\w*)\b"),
        (r"\bmanag(?:e|ed|es|ing)\b|\bmanager (?:of|for)\b", r"\bmanag\w*\b"),
        (r"\bsupervis\w*\b", r"\bsupervis\w*\b"),
        (r"\bheaded\b|\bhead(?:ed)? (?:of|up)\b", r"\bhead\w*\b"),
        (r"\bdirected\b|\bdirector (?:of|for)\b|\bdirect reports?\b", r"\bdirect\w*\b"),
        (r"\boversaw\b|\boversee\w*\b", r"\bovers(?:aw|ee\w*)\b"),
        (r"\bspearhead\w*\b", r"\bspearhead\w*\b"),
        (r"\bowned\b|\bownership\b|\bowner of\b", r"\bown(?:ed|er|ership)\b"),
        (r"\bin charge of\b", r"\bin charge\b"),
        (r"\bresponsible for\b", r"\bresponsib\w*\b"),
        (r"\bmentor\w*\b|\bcoach(?:ed|ing)\b", r"\b(?:mentor|coach)\w*\b"),
        (r"\b(?:co )?found(?:ed|er)\b", r"\bfound(?:ed|er)\b"),
        (r"\bchair(?:ed|ing)\b", r"\bchair\w*\b"),
    )
)
_PEOPLE = re.compile(r"\b(?:team|teams|people|staff|analysts?|engineers?|developers?|reports|interns?|members?|"
                     r"employees?|colleagues|groups?|students?|volunteers?)\b")

#: Common tools / technical skills (normalized). Mentioning one in a claim needs a matching profile skill or fact.
_TOOLS = frozenset("""
python|sql|mysql|postgresql|postgres|sqlite|oracle|sql server|t sql|pl sql|nosql|snowflake|dbt|databricks|spark|
pyspark|hadoop|hive|presto|trino|kafka|airflow|dagster|prefect|tableau|power bi|powerbi|looker|looker studio|qlik|
qlikview|qlik sense|metabase|superset|excel|vba|google sheets|sas|sas enterprise guide|spss|stata|matlab|minitab|
java|javascript|typescript|scala|kotlin|swift|ruby|php|rust|golang|perl|html|css|react|angular|vue|node js|nodejs|
django|flask|fastapi|spring boot|aws|amazon web services|azure|gcp|google cloud|bigquery|redshift|athena|s3|ec2|
emr|aws glue|sagemaker|vertex ai|docker|kubernetes|terraform|jenkins|git|github|gitlab|bitbucket|jira|confluence|
salesforce|sap|hubspot|servicenow|workday|netsuite|mongodb|cassandra|redis|elasticsearch|dynamodb|pandas|numpy|
scipy|scikit learn|sklearn|tensorflow|pytorch|keras|xgboost|hugging face|langchain|openai|chatgpt|llm|llms|
machine learning|deep learning|nlp|natural language processing|computer vision|cognos|ibm cognos|db2|alteryx|
informatica|talend|ssis|ssrs|ssas|sharepoint|power automate|power apps|figma|google analytics|adobe analytics|
mixpanel|amplitude|linux|unix|bash|powershell|smartsheet|asana|trello|dataiku|knime|rapidminer|plotly|
matplotlib|seaborn|ggplot2|ggplot|shiny|etl|a b testing|data warehousing|data modeling|data engineering|
statistics|statistical modeling|forecasting|devops|ci cd|microsoft fabric|azure data factory|ssms|visio
""".replace("\n", "").split("|"))
_TOOLS_RAW = re.compile(r"(?<![\w+#.])(?:c\+\+|c#|f#|\.net)(?![\w+#])", re.I)
_R_RAW = re.compile(r"(?<![\w&'.-])R(?![\w&'-])")
#: "experience with X", "proficient in X", ... -> capitalized words in X are treated as tool names.
_SKILL_PHRASE = re.compile(
    r"\b(?:experience|experienced|proficient|proficiency|skilled|skills|expertise|knowledge|familiar|familiarity|"
    r"fluent|certified|certification|background|expert)\s+(?:with|in|of|using|on)\s+((?:\S+\s*){1,6})", re.I)
_NOT_TOOLS = frozenset({"i", "the", "a", "an", "my", "our", "your", "their", "this", "that", "these", "it", "we",
                        "and", "or", "both", "several", "many", "various"})


def _fact_parts(facts: Iterable[Any]) -> tuple[list[str], list[str]]:
    texts: list[str] = []
    skills: list[str] = []
    for fact in facts:
        if isinstance(fact, str):
            texts.append(fact)
            continue
        texts.append(str(getattr(fact, "text", "") or ""))
        skills.extend(str(s) for s in (getattr(fact, "skills", None) or ()))
    return texts, skills


def claim_sentences(text: str | None) -> list[str]:
    """Sentences that assert something about the candidate's own experience."""
    out = []
    for raw in _SENTENCE_SPLIT.split(normalize_text(text)):
        norm = normalize(raw)
        if not norm:
            continue
        first_person = bool(_FIRST_PERSON.intersection(norm.split()))
        if (first_person and _CLAIM_CUE.search(norm)) or _CLAIM_START.search(norm):
            out.append(raw.strip())
    return out


def tools_mentioned(text: str | None) -> list[str]:
    """Tool / technology names in ``text`` (vocabulary matches plus names after "experience with ...")."""
    text = normalize_text(text)
    norm = f" {normalize(text)} "
    found = [t for t in _TOOLS if f" {t} " in norm]
    found = [t for t in found if not any(t != o and f" {t} " in f" {o} " for o in found)]  # keep the longest
    found += [m.group(0).lower() for m in _TOOLS_RAW.finditer(text)]
    if _R_RAW.search(text):
        found.append("r")
    for m in _SKILL_PHRASE.finditer(text):
        for word in re.split(r"[\s,/&]+", m.group(1)):
            word = word.strip(".;:!?()[]\"'")
            if word and (word[0].isupper() or any(c.isupper() for c in word[1:])) and word.lower() not in _NOT_TOOLS:
                name = normalize(word)
                if name and not any(name in t.split() for t in found):
                    found.append(name)
    return sorted(set(found))


def _tool_supported(tool: str, skills: set[str], fact_norms: list[str]) -> bool:
    padded = f" {tool} "
    return (tool in skills or any(padded in f" {s} " for s in skills)
            or any(padded in f" {f} " for f in fact_norms))


def without_items(text: str, items: Iterable[str]) -> str:
    """``text`` with each (short) trusted context item removed."""
    for item in items:
        if item and len(item) <= 200:
            text = re.sub(re.escape(item), " ", text, flags=re.I)
    return text


#: Words that make a sentence about you a credential / education / background claim.
_IDENTITY_CUE = re.compile(
    r"\b(?:hold|holds|held|holding|graduat\w*|alumn\w*|degrees?|diplomas?|phd|ph d|doctorate|doctoral|mba|masters?|"
    r"bachelors?|certif\w*|licen[cs]\w*|accredited|as an?|am an?|was an?|time at|tenure|stint|interned|internship|"
    r"served|studied|studying|student|pursuing|pursue|enrolled|attended|majored|minored|joined|spent|former|"
    r"formerly|ex|awards?|awarded|won)\b"
)
_IDENTITY_PERSON = _FIRST_PERSON | {"me"}
_SPELLED_NUMBER = re.compile(
    r"(?<![A-Za-z])(?:(?P<tens>twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)"
    r"(?:[\s-]+(?P<unit1>two|three|four|five|six|seven|eight|nine|one))?|"
    r"(?P<unit>zero|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
    r"seventeen|eighteen|nineteen)|(?:an?\s+)?(?P<dozen>dozen))(?![A-Za-z])",
    re.IGNORECASE,
)


def _spelled_as_digits(text: str) -> str:
    """Spelled-out counts as digits ("nine or more" -> "9 or more", "about a dozen" -> "about 12")."""
    def repl(m: re.Match[str]) -> str:
        if m.group("dozen"):
            return "12"
        if m.group("tens"):
            return str(_TENS[m.group("tens").lower()] + _UNITS.get((m.group("unit1") or "").lower(), 0))
        return str(_UNITS[m.group("unit").lower()])
    return _SPELLED_NUMBER.sub(repl, text)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(normalize_text(text)) if normalize(s)]


def identity_sentences(text: str | None, *, every: bool = False) -> list[str]:
    """Sentences about you that can carry a credential, education, title or background claim.

    First-person sentences ("I", "my", "me", ...) with an experience or identity
    word ("hold", "graduated", "certified", "as a", "my time at", ...), sentences
    that start like a résumé bullet, or -- ``every`` -- all sentences (form
    answers are about you as a whole).
    """
    from job_agent.resume import validation as rv  # degree abbreviations are case-sensitive ("MS", not "ms")

    out = []
    for raw in _sentences(text or ""):
        norm = normalize(raw)
        person = bool(_IDENTITY_PERSON.intersection(norm.split()))
        cue = (_CLAIM_CUE.search(norm) or _IDENTITY_CUE.search(norm)
               or any(rx.search(raw) for rx in (rv._DEGREE_WORDS, rv._CREDENTIAL, rv._GRADUATION)))
        if every or _CLAIM_START.search(norm) or (person and cue):
            out.append(raw)
    return out


def verified_education_dates(profile: Any) -> list[str]:
    """The profile's verbatim education start / end dates ("Fall 2025", "May 2027")."""
    return [d for e in (getattr(profile, "education", None) or ()) for d in (e.start, e.end) if d and d.strip()]


def draft_claim_problems(text: str | None, *, facts: Iterable[Any] | None = None,
                         skills: Iterable[str] | None = None, context: Iterable[str] = (),
                         profile: Any = None, about_candidate: bool = False) -> list[str]:
    """Reasons a drafted reply / follow-up / form answer asserts something not traceable to the verified profile.

    ``facts`` are verified fact texts (or objects with ``text`` / ``skills``) and
    ``skills`` the profile skill list; ``None`` means unknown, in which case any
    first-person experience claim with numbers, tools or leadership is rejected.
    ``profile`` (a ``CandidateProfile``) supplies the verified education,
    certifications, employers and titles (and the facts / skills when those are
    not given); without it any degree or graduation wording the facts lack is
    rejected. ``context`` holds trusted application data (e.g. the employer, the
    job title, the date you applied): its numbers may be repeated and its names
    named, but never as part of an experience claim ("my time at Acme").
    ``about_candidate`` treats every sentence as a statement about you (form
    answers).

    Rejected: work-authorization and other legal statements (also misspelled or
    in another language), numerals that are not ASCII digits ("⑳", "Ⅻ"),
    letters in a script these checks cannot read, and in sentences about you
    numbers / tools / leadership not in the facts, numbers whose qualifier or unit
    differs from the fact ("9 or more", "more than 9", "9×" for a plain 9),
    certifications or degrees not in the profile, an expected degree presented as
    completed ("I graduated", "I hold an MS"), and organizations or job titles
    the profile does not contain ("As a Senior Data Scientist at Google"). The
    credential / number / name rules are ``resume.validation``'s (imported
    lazily). Everything is checked on ``normalize_text(text)``; callers store
    that normalized text.
    """
    from job_agent.resume import validation as rv  # lazy: keeps this module free of feature imports at load time

    raw = text or ""
    text = normalize_text(raw)
    context = tuple(normalize_text(c) for c in context if c)
    if profile is not None:
        facts = profile.all_facts() if facts is None else facts
        skills = profile.skills if skills is None else skills
    problems: list[str] = []
    if has_non_ascii_numerals(raw):
        problems.append("uses numerals that are not plain digits (circled, roman, superscript or other scripts)")
    if mentions_work_authorization(text) or _legal_topic(text):
        problems.append("makes a work-authorization or other legal statement")
    if has_unreadable_letters(without_items(text, context)):
        problems.append("contains letters in a script these checks cannot read")
    known = facts is not None
    fact_texts, fact_skills = _fact_parts(facts or ())
    fact_norms = [normalize(t) for t in fact_texts]
    allowed_skills = {n for n in (normalize(s) for s in [*(skills or ()), *fact_skills]) if n}
    fact_numbers: set[str] = set().union(*(numbers_mentioned(t) for t in fact_texts)) if fact_texts else set()
    context_numbers: set[str] = set().union(*(numbers_mentioned(c) for c in context)) if context else set()

    # Verbatim education dates ("expected May 2027") are verified profile data, not new numbers.
    dates = verified_education_dates(profile)
    extra = sorted(numbers_mentioned(without_items(text, dates)) - fact_numbers - context_numbers)
    if extra:
        problems.append(f"mentions numbers not found in your verified facts ({', '.join(extra)})")
    verified = [*fact_texts, *fact_skills, *(skills or ())]
    problems += rv.own_experience_problems(text, verified_texts=verified, profile=profile)
    # Application data (employer, job title) is not a claim: "Engineering Manager" names the role.
    stripped = without_items(text, context)
    for sentence in identity_sentences(stripped, every=about_candidate):
        problems += rv.credential_claim_problems(sentence, verified_texts=verified, profile=profile,
                                                 context=context)
    for sentence in _sentences(stripped):
        norm = normalize(sentence)
        if about_candidate or _IDENTITY_PERSON.intersection(norm.split()) or _CLAIM_START.search(norm):
            multipliers = sorted({w for w in _MULTIPLIER_WORDS.findall(norm)
                                  if not any(re.search(rf"\b{w}\b", f) for f in fact_norms)})
            if multipliers:
                problems.append(f"makes an experience claim with quantities not in your verified facts "
                                f"({', '.join(multipliers)})")
    digit_facts = [_spelled_as_digits(t) for t in fact_texts]
    claims = _sentences(stripped) if about_candidate else claim_sentences(stripped)
    for sentence in claims:
        norm = normalize(sentence)
        numbers = numbers_mentioned(without_items(sentence, dates)) | vague_quantities(sentence)
        tools = tools_mentioned(sentence)
        leadership = [(claim, stem) for claim, stem in _LEADERSHIP if claim.search(norm)]
        if not known:
            if numbers:
                problems.append("makes an experience claim with numbers that cannot be checked against your "
                                "verified facts")
            if tools:
                problems.append(f"makes an experience claim about tools/skills ({', '.join(tools)}) that cannot be "
                                "checked against your profile")
            if leadership:
                problems.append("claims leadership or ownership that cannot be checked against your verified facts")
            continue
        bad_numbers = sorted(n for n in numbers if n not in fact_numbers
                             and not (" " in n and any(f" {n} " in f" {f} " for f in fact_norms)))
        if bad_numbers:
            problems.append(f"makes an experience claim with quantities not in your verified facts "
                            f"({', '.join(bad_numbers)})")
        # A verified value with another qualifier or unit ("9 or more", "about a dozen", "9×" for a plain 9).
        changed = rv.draft_number_problems(_spelled_as_digits(without_items(sentence, dates)), digit_facts)
        if changed:
            problems.append(f"changes how a verified number is qualified or counted ({'; '.join(changed)})")
        bad_tools = [t for t in tools if not _tool_supported(t, allowed_skills, fact_norms)]
        if bad_tools:
            problems.append(f"claims tools/skills not in your profile ({', '.join(bad_tools)})")
        people = bool(_PEOPLE.search(norm))
        for _claim, stem in leadership:
            if not any(stem.search(f) and (not people or _PEOPLE.search(f)) for f in fact_norms):
                problems.append("claims leadership or ownership not found in your verified facts")
                break
    return list(dict.fromkeys(problems))
