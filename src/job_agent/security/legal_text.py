"""Deterministic detection of legal / eligibility questions and risky drafted claims.

Used wherever a legal declaration or an unverified claim could slip through:

* ``is_legal_question`` -- application form fields (``applications.field_mapping``)
  and the local-LLM answer drafter (``llm.generation.draft_answer``);
* ``mentions_work_authorization`` / ``mentions_legal_matter`` -- statements in
  drafted replies and follow-ups ("I am authorized to work for any employer");
* ``draft_claim_problems`` -- drafted text that asserts numbers, tools/skills or
  leadership that do not trace to the verified profile.

The rules are conservative on purpose: a false positive only sends a question
to you for review (or replaces an LLM draft by the safe template); a false
negative could let a work-authorization, sponsorship, citizenship, export-control,
clearance or criminal-history declaration be answered or asserted without you.

No feature-module imports and no model calls: plain regular expressions over
text normalized to lowercase ASCII words separated by single spaces.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Any

# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #
#: Cyrillic / Greek letters that look like Latin ones (homoglyph obfuscation).
_CONFUSABLES = str.maketrans({
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c", "т": "t",
    "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "ӏ": "l", "һ": "h", "ԛ": "q", "ԝ": "w",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T",
    "У": "Y", "Х": "X", "І": "I", "Ј": "J", "Ѕ": "S",
    "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t", "υ": "u",
    "χ": "x", "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N",
    "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
})


def normalize(text: str | None) -> str:
    """Lowercase ASCII words separated by one space ("U.S." -> "u s", "H-1B" -> "h 1b")."""
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_text.lower()).split())


def _prepared(text: str | None) -> str:
    return unicodedata.normalize("NFKC", (text or "").translate(_CONFUSABLES))


def _variants(text: str | None) -> set[str]:
    """Normalized forms with invisible format characters removed *and* replaced by spaces.

    "spon\\u200bsorship" must still read "sponsorship", and "authorized\\u200bto work"
    must still read "authorized to work"; checking both forms covers both tricks.
    """
    prepared = _prepared(text)
    joined = "".join(ch for ch in prepared if unicodedata.category(ch) != "Cf")
    spaced = "".join(" " if unicodedata.category(ch) == "Cf" else ch for ch in prepared)
    return {normalize(joined), normalize(spaced)}


def has_unreadable_letters(text: str | None) -> bool:
    """Letters that survive neither homoglyph mapping nor accent stripping (e.g. non-Latin scripts).

    Such text cannot be checked by the English patterns here, so callers treat it
    as possibly legal (review) rather than assuming it is harmless.
    """
    decomposed = unicodedata.normalize("NFKD", _prepared(text))
    return any(ord(ch) > 127 and unicodedata.category(ch).startswith("L") for ch in decomposed)


def _compile(parts: Iterable[str]) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in parts))


# --------------------------------------------------------------------------- #
# Legal / eligibility topics (applied to normalized text)
# --------------------------------------------------------------------------- #
_US = r"(?:u s(?: a)?|usa|us|united states(?: of america)?|america)"
_VISA_TYPES = r"\b(?:[fjm] ?1|h ?1 ?b ?1?|h ?1|h ?2 ?[ab]|h ?4|l ?1 ?[ab]?|o ?1 ?[ab]?|e ?3|tn (?:visa|status|holder|classification))\b"

#: Work authorization, sponsorship and immigration (shared by questions and statements).
_WORK_AUTH_CORE = (
    r"\b(?:authori[sz]\w*|eligib\w*|permit\w*|permission|allowed|entitled|legally|lawfully|right)"
    r"(?: \w+){0,3} (?:work|working|employment|employed|employ)\b",
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
    r"\bresident aliens?\b", r"\baliens?\b", r"\basyl\w*\b", r"\brefugees?\b", r"\bdaca\b", r"\bdeferred action\b",
    r"\btemporary protected status\b", r"\btps\b",
    r"\bcitizen\w*\b", r"\bnationality\b", rf"\b(?:{_US}|american|dual|foreign) nationals?\b", r"\bnational of\b",
    r"\bprotected individual\b", r"\b1324b\b", r"\b8 u s c\b",
    r"\bchange (?:of|in) (?:\w+ )?status\b", r"\bstatus (?:change|transfer)\b",
    r"\b(?:visa|petition|status|h ?1 ?b) transfer\b", r"\btransfer (?:of )?(?:your |my )?(?:visa|status|petition|h ?1 ?b)\b",
    r"\bcap (?:exempt|gap|subject)\b",
    r"\bi ?9\b", r"\bemployment eligibility\b", r"\be ?verify\b",
    r"\bproof of (?:identity|eligibility|citizenship|status|work|employment)\b",
    # a few non-English forms of the same questions
    r"\bautori[sz]ad\w*\b", r"\bpatrocin\w*\b", r"\bciudadan\w*\b", r"\bpermiso de trabajo\b", r"\bnacionalidad\b",
    r"\bautoris\w*\b", r"\bparrain\w*\b", r"\bcitoyen\w*\b", r"\bpermis de travail\b", r"\barbeitserlaubnis\b",
    r"\bstaatsangeh\w*\b", r"\baufenthalt\w*\b",
)

_LEGAL_QUESTION_EXTRA = (
    r"\beligib\w*\b",
    r"\b(?:legally|lawfully|lawful)\b",
    r"\blegal\b(?! (?:first |last |middle |full |given |family )?(?:name|names|surname)\b)",
    r"\bable to (?:legally |lawfully )?work\b(?=(?: \w+){0,6} (?:in|within|for|without|anywhere|legally|lawfully|"
    r"permanently)\b)",
    r"\b(?:can|could|may) you (?:legally |lawfully )?(?:work|be employed)(?: \w+){0,2} (?:in|within|for)\b",
    rf"\b(?:work|working|employed|employment)(?: \w+){{0,2}} (?:in|within) (?:the )?{_US}\b",
    # security clearance
    r"\bclearances?\b", r"\bts ?sci\b", r"\bsci\b", r"\b(?:top )?secret\b", r"\bpublic trust\b", r"\bpolygraph\b",
    r"\bsf ?86\b", r"\bsuitability determination\b",
    # export control
    r"\bexport\w*\b", r"\bitar\b", r"\bear\b", rf"\b{_US} persons?\b", r"\bdeemed exports?\b", r"\bofac\b",
    r"\bsanction\w*\b", r"\bembargo\w*\b", r"\bforeign (?:national|nationals|person|persons|government|principal)\b",
    # criminal history / background checks
    r"\bcriminal\w*\b", r"\bconvict\w*\b", r"\bfelon\w*\b", r"\bmisdemean\w*\b", r"\barrest\w*\b", r"\bguilty\b",
    r"\bno contest\b", r"\bnolo contendere\b",
    r"\bbackground (?:check|checks|investigation|investigations|screening|screenings|verification)\b",
    r"\bcharged with\b", r"\boffen[cs]es?\b", r"\bdrug (?:test|tests|testing|screen|screening)\b", r"\bdebar\w*\b",
    r"\bexcluded from\b",
    # restrictive covenants, relatives / conflicts of interest
    r"\bnon ?compet\w*\b", r"\bnoncompet\w*\b", r"\bnon ?solicit\w*\b", r"\bnonsolicit\w*\b",
    r"\brestrictive covenants?\b", r"\bbound by\b",
    r"\bagreements? (?:that|which) (?:would |could |may |might )?(?:prevent|restrict|limit|prohibit)\b",
    r"\bprevent (?:you|me) from (?:working|being employed|accepting|performing|joining)\b",
    r"\brelatives?\b", r"\bfamily members?\b", r"\brelated to (?:any|an|a|someone|anyone)\b",
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
    rf"\b{_US} persons?\b", r"\bclearances?\b", r"\bitar\b", r"\bexport control\w*\b",
)

LEGAL_QUESTION_RE = _compile(_WORK_AUTH_CORE + _LEGAL_QUESTION_EXTRA)
WORK_AUTH_STATEMENT_RE = _compile(_WORK_AUTH_CORE + _WORK_AUTH_STATEMENT_EXTRA)

#: Compensation, EEO and identity questions: not legal declarations, but never drafted by the LLM either.
SENSITIVE_QUESTION_RE = re.compile(
    r"\b(?:salary|salaries|compensation|pay|paid|wages?|remuneration|bonus|equity|stipend|hourly rate|rate of pay|"
    r"desired (?:annual )?base|base pay|total cash|ote|disabilit\w*|disabled|veterans?|military|armed forces|gender|"
    r"sex|race|racial|ethnic\w*|hispanic|latin[oax]|sexual orientation|lgbt\w*|transgender|pronouns?|"
    r"date of birth|birth ?date|dob|age|religio\w*|marital|pregnan\w*|social security|ssn|national origin|"
    r"passport|driver s licen[cs]e)\b"
)


def is_legal_question(text: str | None) -> bool:
    """Does this question (label, field name, help text) concern a legal / eligibility declaration?

    Covers work authorization and eligibility, sponsorship and visas (F-1, J-1,
    H-1B, TN, O-1, OPT/CPT/STEM OPT, EAD, petitions, change of status),
    citizenship / nationality / permanent residence, protected-individual status,
    security clearances, export control (ITAR/EAR, U.S. person), criminal history
    and background checks, non-compete agreements and relatives / conflicts of
    interest. Text in a script these patterns cannot read counts as legal.
    """
    if not (text or "").strip():
        return False
    if has_unreadable_letters(text):
        return True
    return any(LEGAL_QUESTION_RE.search(v) for v in _variants(text) if v)


def is_sensitive_question(text: str | None) -> bool:
    """Compensation / EEO / identity questions (answered by you, never drafted)."""
    return any(SENSITIVE_QUESTION_RE.search(v) for v in _variants(text) if v)


def mentions_work_authorization(text: str | None) -> bool:
    """Does this text *state* something about work authorization, sponsorship or immigration status?

    E.g. "I am eligible to work for any employer", "I will not need any employer
    support", "authorized to work in the United States", "without sponsorship".
    """
    if not (text or "").strip():
        return False
    return any(WORK_AUTH_STATEMENT_RE.search(v) for v in _variants(text) if v)


def mentions_legal_matter(text: str | None) -> bool:
    """Work-authorization statement or any legal topic (clearance, export control, criminal history, ...)."""
    return mentions_work_authorization(text) or is_legal_question(text)


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


def _canon(number: str) -> str:
    value = number.replace(",", "")
    if "." in value:
        return value.rstrip("0").rstrip(".") or "0"
    return str(int(value)) if value.isdigit() else value


def numbers_mentioned(text: str | None) -> set[str]:
    """Quantities in ``text`` as canonical digit strings ("three" -> "3", "1,200" -> "1200", "30%" -> "30").

    "one" only counts before a time unit ("one year"), so "one of the teams" is not a number.
    """
    found = {_canon(m.group(0)) for m in _DIGITS_RE.finditer(text or "")}
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
    r"responsib(?:le|ility|ilities)|in charge)\b"
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
    for raw in _SENTENCE_SPLIT.split(text or ""):
        norm = normalize(raw)
        if not norm:
            continue
        first_person = bool(_FIRST_PERSON.intersection(norm.split()))
        if (first_person and _CLAIM_CUE.search(norm)) or _CLAIM_START.search(norm):
            out.append(raw.strip())
    return out


def tools_mentioned(text: str | None) -> list[str]:
    """Tool / technology names in ``text`` (vocabulary matches plus names after "experience with ...")."""
    norm = f" {normalize(text)} "
    found = [t for t in _TOOLS if f" {t} " in norm]
    found = [t for t in found if not any(t != o and f" {t} " in f" {o} " for o in found)]  # keep the longest
    found += [m.group(0).lower() for m in _TOOLS_RAW.finditer(text or "")]
    if _R_RAW.search(text or ""):
        found.append("r")
    for m in _SKILL_PHRASE.finditer(text or ""):
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


def draft_claim_problems(text: str | None, *, facts: Iterable[Any] | None = None,
                         skills: Iterable[str] | None = None, context: Iterable[str] = ()) -> list[str]:
    """Reasons a drafted reply / follow-up asserts something not traceable to the verified profile.

    ``facts`` are verified fact texts (or objects with ``text`` / ``skills``) and
    ``skills`` the profile skill list; ``None`` means unknown, in which case any
    first-person experience claim with numbers, tools or leadership is rejected.
    ``context`` holds trusted application data (e.g. the date you applied) whose
    numbers may be repeated, but never as part of an experience claim.

    Work-authorization and other legal statements are always rejected.
    """
    text = text or ""
    problems: list[str] = []
    if mentions_legal_matter(text):
        problems.append("makes a work-authorization or other legal statement")
    known = facts is not None
    fact_texts, fact_skills = _fact_parts(facts or ())
    fact_norms = [normalize(t) for t in fact_texts]
    allowed_skills = {n for n in (normalize(s) for s in [*(skills or ()), *fact_skills]) if n}
    fact_numbers: set[str] = set().union(*(numbers_mentioned(t) for t in fact_texts)) if fact_texts else set()
    context_numbers: set[str] = set().union(*(numbers_mentioned(c) for c in context)) if context else set()

    extra = sorted(numbers_mentioned(text) - fact_numbers - context_numbers)
    if extra:
        problems.append(f"mentions numbers not found in your verified facts ({', '.join(extra)})")
    # Application data (employer, job title) is not a claim: "Engineering Manager" names the role.
    claim_text = text
    for item in context:
        if item and len(item) <= 200:
            claim_text = re.sub(re.escape(item), " ", claim_text, flags=re.I)
    for sentence in claim_sentences(claim_text):
        norm = normalize(sentence)
        numbers = numbers_mentioned(sentence) | vague_quantities(sentence)
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
        bad_tools = [t for t in tools if not _tool_supported(t, allowed_skills, fact_norms)]
        if bad_tools:
            problems.append(f"claims tools/skills not in your profile ({', '.join(bad_tools)})")
        people = bool(_PEOPLE.search(norm))
        for _claim, stem in leadership:
            if not any(stem.search(f) and (not people or _PEOPLE.search(f)) for f in fact_norms):
                problems.append("claims leadership or ownership not found in your verified facts")
                break
    return list(dict.fromkeys(problems))
