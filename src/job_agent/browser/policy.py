"""Where the browser may go.

Rules (deliberately conservative):

* The browser *opens* only ``http``/``https`` URLs whose host equals the host of
  the job's stored ``application_url`` or ``job_url`` (exact hostname match;
  ``www.example.com`` and ``example.com`` are different hosts).
* While on the page, top-level navigations and redirects may go to those same
  hosts, or to a known ATS host of the job's *own* source (Greenhouse / Lever /
  Ashby) **only if** the job's board identifier appears in that URL's path or
  query, i.e. it is the same employer's posting on the same ATS.
* A host whose stored URL used ``https`` may not be downgraded to ``http``.
* Everything else (other domains, SSO pages, ``data:`` URLs...) is refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlsplit

from job_agent.browser.errors import NavigationBlocked

#: ATS hosts per job source. Only used for navigations *after* the official page
#: was opened, and only together with the posting's board identifier.
KNOWN_ATS_HOSTS: dict[str, frozenset[str]] = {
    "greenhouse": frozenset({"boards.greenhouse.io", "job-boards.greenhouse.io", "boards.eu.greenhouse.io",
                             "job-boards.eu.greenhouse.io"}),
    "lever": frozenset({"jobs.lever.co", "jobs.eu.lever.co"}),
    "ashby": frozenset({"jobs.ashbyhq.com"}),
}

#: Non-network pseudo URLs a page may legitimately show (blank page, Chromium's
#: error page after we aborted a request).
_BENIGN_PREFIXES = ("about:blank", "about:srcdoc", "chrome-error://")


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".")


@dataclass(frozen=True)
class NavigationPolicy:
    allowed_hosts: frozenset[str]
    https_hosts: frozenset[str] = field(default_factory=frozenset)
    ats_hosts: frozenset[str] = field(default_factory=frozenset)
    ats_identifier: str | None = None

    @classmethod
    def for_job(cls, *, job_url: str | None, application_url: str | None, source_name: str | None = None,
                source_identifier: str | None = None) -> "NavigationPolicy":
        hosts: set[str] = set()
        https: set[str] = set()
        for url in (job_url, application_url):
            if not url:
                continue
            parts = urlsplit(url)
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                continue
            host = parts.hostname.lower().rstrip(".")
            hosts.add(host)
            if parts.scheme == "https":
                https.add(host)
        ats = KNOWN_ATS_HOSTS.get((source_name or "").lower(), frozenset())
        identifier = (source_identifier or "").strip().lower() or None
        return cls(frozenset(hosts), frozenset(https), ats if identifier else frozenset(), identifier)

    # ------------------------------------------------------------------ #
    def _scheme_ok(self, url: str, host: str) -> str | None:
        scheme = urlsplit(url).scheme.lower()
        if scheme not in {"http", "https"}:
            return f"only http(s) URLs are allowed (got {scheme or 'no scheme'!s})"
        if scheme == "http" and host in self.https_hosts:
            return "refusing to downgrade from https to http"
        return None

    def initial_problem(self, url: str) -> str | None:
        """Why ``url`` may not be *opened* (None = allowed)."""
        host = _host(url)
        if not host:
            return "URL has no host"
        problem = self._scheme_ok(url, host)
        if problem:
            return problem
        if host not in self.allowed_hosts:
            allowed = ", ".join(sorted(self.allowed_hosts)) or "none stored"
            return f"host {host!r} is not the job's official host ({allowed})"
        return None

    def navigation_problem(self, url: str) -> str | None:
        """Why a top-level navigation / redirect to ``url`` is refused (None = allowed)."""
        if url.startswith(_BENIGN_PREFIXES):
            return None
        host = _host(url)
        if not host:
            return "URL has no host"
        problem = self._scheme_ok(url, host)
        if problem:
            return problem
        if host in self.allowed_hosts:
            return None
        if host in self.ats_hosts and self._mentions_identifier(url):
            return None
        return f"host {host!r} is not the job's official host or its ATS posting"

    def request_host_allowed(self, url: str) -> bool:
        """For strict (offline) mode: may *any* request go to this URL's host?"""
        if url.startswith(_BENIGN_PREFIXES) or url.startswith(("data:", "blob:")):
            return True
        return self.navigation_problem(url) is None

    def _mentions_identifier(self, url: str) -> bool:
        if not self.ats_identifier:
            return False
        parts = urlsplit(url)
        segments = [s.lower() for s in parts.path.split("/") if s]
        values = [v.lower() for _, v in parse_qsl(parts.query, keep_blank_values=False)]
        return self.ats_identifier in segments or self.ats_identifier in values

    def check_initial(self, url: str) -> None:
        problem = self.initial_problem(url)
        if problem:
            raise NavigationBlocked(url, problem)

    def check_navigation(self, url: str) -> None:
        problem = self.navigation_problem(url)
        if problem:
            raise NavigationBlocked(url, problem)
