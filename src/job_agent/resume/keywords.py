"""Deterministic extraction of job keywords / requirements from a listing.

Sources (with weights): title (3), required qualifications (2), preferred
qualifications (1.5), description (1), plus a boost for terms in the scoring
module's ``matching_qualifications`` when they also occur in the listing. Two
kinds of keywords come out:

* ``skills``: canonical lexicon tools/skills mentioned in the listing, and
* ``terms``: lowercase noun-phrase-like n-grams (1-3 words without stopwords).

All listing text is untrusted. Keywords are only ever used to *rank and select*
verified facts and to name a requirement that literally appears in the listing;
they are never copied into a document as a claim about the candidate.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from job_agent.resume import lexicon
from job_agent.resume.text import GENERIC_TERMS, STOPWORDS, content_set, mentions, normalize_space, stem, words
from job_agent.security.untrusted import clean_untrusted
from job_agent.security.untrusted import injection_suspected as _injection_suspected

_TITLE_W, _REQ_W, _PREF_W, _DESC_W, _MATCH_BOOST = 3.0, 2.0, 1.5, 1.0, 1.5
_MAX_TERMS = 60
_SAFE_KEYWORD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .+#/&-]{0,40}$")


@dataclass
class JobKeywords:
    title: str
    skills: list[str] = field(default_factory=list)  # canonical lexicon names, by weight
    skill_weights: dict[str, float] = field(default_factory=dict)
    terms: dict[str, float] = field(default_factory=dict)  # lowercase phrase -> weight
    requirements: list[str] = field(default_factory=list)  # cleaned qualification lines
    text: str = ""  # full cleaned listing text (title + qualifications + description)
    injection_suspected: bool = False
    _stems: set[str] = field(default_factory=set, repr=False)

    def mentions(self, phrase: str) -> bool:
        """Does the listing literally mention ``phrase`` (word boundary, case-insensitive)?"""
        return mentions(self.text, phrase)

    def has_stem(self, word: str) -> bool:
        return stem(word) in self._stems

    def top_terms(self, n: int = 15) -> list[str]:
        return [t for t, _ in sorted(self.terms.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]

    def safe_keywords(self, n: int = 15, *, trusted_text: str) -> list[str]:
        """Keywords that are safe to hand to the local LLM as hints.

        Only listing keywords that also occur in ``trusted_text`` (the verified
        profile) qualify, so no listing-originated free text (e.g. injected
        instructions or skills the candidate lacks) reaches the model this way.
        """
        trusted_words = {w.lower() for w in words(trusted_text)}
        out: list[str] = []
        for s in self.skills:
            if s not in out and mentions(trusted_text, s):
                out.append(s)
        for t in self.top_terms(n * 3):
            if len(out) >= n:
                break
            if (_SAFE_KEYWORD.match(t) and len(t.split()) <= 3 and not _injection_suspected(t)
                    and all(w in trusted_words for w in t.split()) and t not in out):
                out.append(t)
        return out[:n]


def _ngrams(text: str, max_n: int = 3) -> list[str]:
    """Stopword-delimited 1..3-gram phrases (lowercase)."""
    phrases: list[str] = []
    for chunk in re.split(r"[,;:()\[\]\n.!?•·|]+", text):
        run: list[str] = []
        for w in words(chunk):
            lw = w.lower()
            if lw in STOPWORDS or len(lw) < 2 or lw.isdigit():
                phrases.extend(_runs(run, max_n))
                run = []
            else:
                run.append(lw)
        phrases.extend(_runs(run, max_n))
    return phrases


def _runs(run: list[str], max_n: int) -> list[str]:
    out = []
    for n in range(1, max_n + 1):
        for i in range(len(run) - n + 1):
            gram = run[i : i + n]
            if n == 1 and (gram[0] in GENERIC_TERMS or len(gram[0]) < 3):
                continue
            if gram[0] in GENERIC_TERMS or gram[-1] in GENERIC_TERMS:
                continue
            out.append(" ".join(gram))
    return out


def extract_job_keywords(title: str, required: list[str] | None = None, preferred: list[str] | None = None,
                         description: str = "", *, emphasis: list[str] | None = None) -> JobKeywords:
    title_c = normalize_space(clean_untrusted(title, max_chars=300))
    req = [normalize_space(clean_untrusted(r, max_chars=1000)) for r in (required or []) if r and r.strip()]
    pref = [normalize_space(clean_untrusted(p, max_chars=1000)) for p in (preferred or []) if p and p.strip()]
    desc = clean_untrusted(description, max_chars=40_000)
    full_text = "\n".join([title_c, *req, *pref, desc])

    sources: list[tuple[str, float]] = [(title_c, _TITLE_W)] + [(r, _REQ_W) for r in req] + [
        (p, _PREF_W) for p in pref] + [(desc, _DESC_W)]

    skill_w: dict[str, float] = defaultdict(float)
    term_w: dict[str, float] = defaultdict(float)
    for text, weight in sources:
        for name in lexicon.find_skills(text):
            skill_w[name] += weight
        for gram in set(_ngrams(text)):
            # Longer phrases are more specific; weight them a little higher.
            term_w[gram] += weight * (1.0 + 0.25 * (gram.count(" ")))

    for line in emphasis or []:
        line_c = normalize_space(clean_untrusted(line, max_chars=500))
        for name in lexicon.find_skills(line_c):
            if name in skill_w:
                skill_w[name] += _MATCH_BOOST
        for gram in set(_ngrams(line_c)):
            if gram in term_w:
                term_w[gram] += _MATCH_BOOST

    terms = dict(sorted(term_w.items(), key=lambda kv: (-kv[1], kv[0]))[:_MAX_TERMS])
    skills = [k for k, _ in sorted(skill_w.items(), key=lambda kv: (-kv[1], lexicon.lexicon_names().index(kv[0])))]
    return JobKeywords(
        title=title_c,
        skills=skills,
        skill_weights=dict(skill_w),
        terms=terms,
        requirements=req + pref,
        text=full_text,
        injection_suspected=_injection_suspected(full_text),
        _stems=content_set(full_text),
    )
