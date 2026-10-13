"""LLM-specific errors (all subclasses of the ``job_agent.errors`` hierarchy)."""

from __future__ import annotations

from job_agent.errors import PermanentError, RecoverableError


class LLMUnavailable(RecoverableError):
    """The Ollama server, or the requested model, cannot be used right now.

    Recoverable: everything falls back to rules-based behaviour and a later run
    may succeed once Ollama is started / the model is pulled.
    """

    human_action = ("Start Ollama (`ollama serve`) and pull the configured model "
                    "(`job-agent llm check` shows what is missing; see docs/local-llm.md).")


class LLMInvalidOutput(PermanentError):
    """The model's output failed validation even after one corrective re-ask."""


class LegalQuestionRefused(PermanentError):
    """A legal / eligibility / compensation question that must be answered by the human."""

    human_action = "Answer this question yourself; legal and eligibility answers are never generated."
