"""Optional local-LLM assistance behind the deterministic validators.

Uses only the contracts ``job_agent.llm.client.get_client(ctx)``,
``job_agent.llm.generation.reword_fact`` and
``job_agent.llm.generation.draft_cover_letter`` (imported lazily). When the llm
module is absent, disabled, or fails, generation silently falls back to the
verbatim facts / template letter -- the failure is recorded, never hidden.

Security: the facts given to the model are verified profile text; the job title
and keywords are sanitized hints (the llm module wraps them as untrusted data).
Whatever the model returns is accepted only if ``validate_claim`` reports no
issue, so prompt-injection in a job description cannot add content. The stored
bullet is ``validation.claim_text`` of the output: exactly the normalized text
that was validated (never a raw form with full-width or look-alike letters).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from job_agent.errors import JobAgentError, PermanentError
from job_agent.logging_config import get_logger
from job_agent.profile import CandidateProfile
from job_agent.resume.keywords import JobKeywords
from job_agent.resume.models import Issue, RejectedRewording, ResumeContent
from job_agent.resume.text import normalize_space
from job_agent.resume.validation import claim_text, summarize, validate_claim

if TYPE_CHECKING:  # pragma: no cover
    from job_agent.context import AppContext

log = get_logger(__name__)

ErrorSink = Callable[[str, BaseException], None]


@dataclass
class LLMAssist:
    client: Any
    model: str
    on_error: ErrorSink
    failed: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"llm:{self.model}"

    def _fail(self, what: str, exc: BaseException) -> None:
        self.failed = True
        self.notes.append(f"{what} failed ({type(exc).__name__}); verified text used instead")
        self.on_error(what, exc)

    def reword(self, fact_text: str, *, job_title: str, job_keywords: list[str]) -> tuple[str | None, str | None]:
        """``(rewording, None)``, or ``(None, reason)`` when this one output was unusable.

        A ``PermanentError`` (e.g. the llm module's ``LLMInvalidOutput``) only
        rejects this fact's rewording. Unavailability (``RecoverableError``),
        a missing module, or an unexpected bug stops further LLM calls; the
        error is recorded and verbatim facts are used.
        """
        if self.failed:
            return None, None
        try:
            from job_agent.llm.generation import reword_fact

            result = reword_fact(self.client, fact_text, job_title=job_title, job_keywords=job_keywords)
        except PermanentError as exc:
            log.info("llm rewording output rejected by the llm module: %s", type(exc).__name__)
            return None, f"llm_output_invalid: {exc}"
        except Exception as exc:  # ImportError / RecoverableError per contract, or a bug in an optional
            self._fail("llm rewording", exc)  # component: recorded, then verbatim text is used
            return None, None
        if not isinstance(result, str):
            return None, "llm_output_invalid: rewording is not text"
        return result, None

    def draft_cover_letter(self, **kwargs: Any) -> Any:
        if self.failed:
            return None
        try:
            from job_agent.llm.generation import draft_cover_letter

            return draft_cover_letter(self.client, **kwargs)
        except Exception as exc:  # ImportError / JobAgentError per contract: recorded; template letter is used
            self._fail("llm cover letter", exc)
            return None


def _model_name(client: Any, default: str) -> str:
    for attr in ("model", "selected_model"):
        value = getattr(client, attr, None)
        if isinstance(value, str) and value:
            return value
    for method in ("resolve_model", "select_model"):  # resolve_model uses the cached probe (no request)
        fn = getattr(client, method, None)
        if not callable(fn):
            continue
        try:
            value = fn()
        except JobAgentError:
            continue
        if isinstance(value, str) and value:
            return value
    return default


def get_assist(ctx: "AppContext", on_error: ErrorSink) -> tuple[LLMAssist | None, str]:
    """An ``LLMAssist`` if the local LLM is usable, else ``(None, reason)``."""
    try:
        from job_agent.llm.client import get_client
    except ImportError as exc:
        return None, f"local LLM module not available ({exc.name or exc})"
    try:
        client = get_client(ctx)
    except JobAgentError as exc:
        on_error("llm client", exc)
        return None, f"local LLM unavailable: {exc}"
    if client is None:
        return None, "local LLM disabled or not reachable"
    return LLMAssist(client=client, model=_model_name(client, ctx.config.llm.model), on_error=on_error), ""


def reword_bullets(assist: LLMAssist, content: ResumeContent, profile: CandidateProfile, kw: JobKeywords, *,
                   job_title: str, forbidden_names: list[str]) -> list[Issue]:
    """Try to reword each single-fact bullet; keep a rewording only if it validates cleanly."""
    issues: list[Issue] = []
    trusted = "\n".join([*profile.skills, *(f.text for f in profile.all_facts()),
                         *(k for f in profile.all_facts() for k in f.keywords)])
    hints = kw.safe_keywords(15, trusted_text=trusted)
    for section, bullet in content.all_bullets():
        if assist.failed:
            break
        if len(bullet.fact_ids) != 1:
            continue
        fact = profile.fact_by_id(bullet.fact_ids[0])
        if fact is None:
            continue
        raw, invalid = assist.reword(normalize_space(fact.text), job_title=job_title, job_keywords=hints)
        if invalid:
            bullet.rejected_rewording = RejectedRewording(text="", reasons=[invalid[:300]])
            issues.append(Issue(code="rewording_rejected", severity="info", location=section, document="resume",
                                message=f"LLM rewording of fact {fact.id} rejected ({invalid[:200]}); "
                                        "verbatim fact text used"))
            continue
        if raw is None:
            continue
        candidate = normalize_space(raw).strip("\"'“”‘’ ").lstrip("-•* ").strip()
        if not candidate or candidate == bullet.text:
            continue
        # Validation sees the raw text (to reject disguising characters) and judges its normalized form;
        # the normalized form is what gets stored and rendered.
        problems = validate_claim(candidate, [fact], profile, forbidden_names=forbidden_names)
        if problems:
            reasons = summarize(problems)
            bullet.rejected_rewording = RejectedRewording(text=candidate[:300], reasons=reasons[:8])
            issues.append(Issue(code="rewording_rejected", severity="info", location=section, document="resume",
                                message=f"LLM rewording of fact {fact.id} rejected ({reasons[0]}); "
                                        "verbatim fact text used"))
        else:
            bullet.text = claim_text(candidate)
            bullet.generator = assist.label
    for note in assist.notes:
        issues.append(Issue(code="llm_unavailable", severity="info", document="resume", message=note))
    return issues
