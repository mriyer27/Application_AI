"""Exception hierarchy.

Every failure is classified into one of the ``ErrorKind`` categories so the
scheduler, dashboard and reports can tell the user exactly what happened and
what (if anything) they need to do. Never swallow these silently: either handle
them deliberately or record them with ``database.repo.record_error``.
"""

from __future__ import annotations

from job_agent.models import ErrorKind


class JobAgentError(Exception):
    kind: ErrorKind = ErrorKind.PERMANENT
    #: Short instruction for the human, shown in dashboard / reports.
    human_action: str | None = None

    def __init__(self, message: str, *, human_action: str | None = None, retryable: bool | None = None):
        super().__init__(message)
        if human_action is not None:
            self.human_action = human_action
        self._retryable = retryable

    @property
    def retryable(self) -> bool:
        if self._retryable is not None:
            return self._retryable
        return self.kind == ErrorKind.RECOVERABLE


class ConfigError(JobAgentError):
    kind = ErrorKind.HUMAN_ACTION_REQUIRED
    human_action = "Fix the configuration file and run `job-agent doctor`."


class ProfileError(JobAgentError):
    kind = ErrorKind.HUMAN_ACTION_REQUIRED
    human_action = "Fix your candidate profile YAML and run `job-agent doctor`."


class DatabaseError(JobAgentError):
    kind = ErrorKind.PERMANENT


class RecoverableError(JobAgentError):
    """Transient failure (network blip, timeout, 5xx, rate limit). Safe to retry later."""

    kind = ErrorKind.RECOVERABLE


class PermanentError(JobAgentError):
    kind = ErrorKind.PERMANENT


class AuthorizationRequired(JobAgentError):
    """External authorization (e.g. Gmail OAuth) is missing, expired or revoked."""

    kind = ErrorKind.AUTHORIZATION_REQUIRED


class HumanActionRequired(JobAgentError):
    kind = ErrorKind.HUMAN_ACTION_REQUIRED


class UnknownOutcome(JobAgentError):
    """A consequential action may or may not have happened. Never blindly retry."""

    kind = ErrorKind.UNKNOWN_OUTCOME
    human_action = "Check whether the action actually happened before retrying."


class PermissionDenied(JobAgentError):
    kind = ErrorKind.HUMAN_ACTION_REQUIRED


class ApprovalError(JobAgentError):
    """Missing, rejected, expired, already-consumed or mismatched approval."""

    kind = ErrorKind.HUMAN_ACTION_REQUIRED


class InvalidTransition(JobAgentError):
    kind = ErrorKind.PERMANENT


class TaskLocked(JobAgentError):
    """Another run of the same task holds the lock; not an error worth alerting on."""

    kind = ErrorKind.RECOVERABLE


class AutomationPaused(JobAgentError):
    kind = ErrorKind.HUMAN_ACTION_REQUIRED
    human_action = "Automation is paused. Run `job-agent resume` to continue."
