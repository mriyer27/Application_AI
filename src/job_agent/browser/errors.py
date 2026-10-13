"""Browser-layer exceptions (all ``job_agent.errors`` subclasses)."""

from __future__ import annotations

from job_agent.errors import HumanActionRequired, PermanentError, RecoverableError


class ChallengeDetected(HumanActionRequired):
    """A CAPTCHA, human-verification page, login wall or account requirement.

    The automation stops here. It never tries to solve or bypass the check.
    """

    def __init__(self, kind: str, detail: str, url: str) -> None:
        super().__init__(
            f"{kind.replace('_', ' ')} detected at {url}: {detail}",
            human_action=(
                "Open the application page yourself, complete it manually, then run "
                "`job-agent applications mark-submitted <application-uid>`."
            ),
        )
        self.challenge_kind = kind
        self.detail = detail
        self.url = url


class NavigationBlocked(HumanActionRequired):
    """The page tried to open or redirect to a host outside the allowlist."""

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(
            f"navigation to {url} refused: {reason}",
            human_action=(
                "Open the employer's official application page yourself and check where it leads "
                "before entering any data."
            ),
        )
        self.url = url
        self.reason = reason


class PageUnavailable(PermanentError):
    """The application page could not be loaded (404/410, closed listing...)."""


class BrowserUnavailable(RecoverableError):
    """Chromium could not be launched (not installed, no display for headed mode...)."""


class FillError(PermanentError):
    """A value could not be entered into a form control."""
