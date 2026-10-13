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
* Ports count: a URL on an allowed host must use the scheme's default port or a
  port that one of the stored URLs used (``host:8443`` is not ``host``).
* Everything else (other domains, SSO pages, ``data:`` URLs...) is refused.

The session (``session._Guard``) applies this policy to every request, not only
navigations; ``asset_request_allowed`` is the one exception it makes for
third-party hosts once filling has begun: GETs of static files from the job's
ATS hosts and the small per-ATS CDN allowlist ``ATS_ASSET_HOSTS``.
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

#: Static-asset CDNs per ATS. Used only for GET requests of static resource
#: types (scripts, styles, fonts, images, media) after filling has begun, so a
#: page can finish lazy-loading its own assets. Never for navigations, XHR /
#: fetch, or any non-GET request. Keep this list small and limited to hosts the
#: ATS vendor itself operates.
ATS_ASSET_HOSTS: dict[str, frozenset[str]] = {
    "greenhouse": frozenset({"boards.cdn.greenhouse.io", "job-boards.cdn.greenhouse.io"}),
    "lever": frozenset(),
    "ashby": frozenset({"cdn.ashbyprd.com"}),
}

_DEFAULT_PORTS = {"http": 80, "https": 443}

#: Non-network pseudo URLs a page may legitimately show (blank page, Chromium's
#: error page after we aborted a request).
_BENIGN_PREFIXES = ("about:blank", "about:srcdoc", "chrome-error://")


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def _port(url: str) -> int | None:
    """Effective port of an http(s) URL (default port when omitted); None if invalid."""
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        return None
    return port if port is not None else _DEFAULT_PORTS.get(parts.scheme.lower())


@dataclass(frozen=True)
class NavigationPolicy:
    allowed_hosts: frozenset[str]
    https_hosts: frozenset[str] = field(default_factory=frozenset)
    ats_hosts: frozenset[str] = field(default_factory=frozenset)
    ats_identifier: str | None = None
    #: (host, port) pairs of stored URLs that use a non-default port.
    ports: frozenset[tuple[str, int]] = field(default_factory=frozenset)
    #: Static-asset hosts (GET of static files only, see ``asset_request_allowed``).
    asset_hosts: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def for_job(cls, *, job_url: str | None, application_url: str | None, source_name: str | None = None,
                source_identifier: str | None = None) -> "NavigationPolicy":
        hosts: set[str] = set()
        https: set[str] = set()
        ports: set[tuple[str, int]] = set()
        for url in (job_url, application_url):
            if not url:
                continue
            parts = urlsplit(url)
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                continue
            port = _port(url)
            if port is None:
                continue
            host = parts.hostname.lower().rstrip(".")
            hosts.add(host)
            if parts.scheme == "https":
                https.add(host)
            if port != _DEFAULT_PORTS[parts.scheme]:
                ports.add((host, port))
        source = (source_name or "").lower()
        ats = KNOWN_ATS_HOSTS.get(source, frozenset())
        identifier = (source_identifier or "").strip().lower() or None
        assets: set[str] = set(ATS_ASSET_HOSTS.get(source, frozenset()))
        for name, ats_set in KNOWN_ATS_HOSTS.items():
            if hosts & ats_set:  # the stored page itself is on this ATS
                assets |= ATS_ASSET_HOSTS.get(name, frozenset()) | ats_set
        return cls(frozenset(hosts), frozenset(https), ats if identifier else frozenset(), identifier,
                   frozenset(ports), frozenset(assets - hosts))

    # ------------------------------------------------------------------ #
    def _scheme_ok(self, url: str, host: str) -> str | None:
        scheme = urlsplit(url).scheme.lower()
        if scheme not in {"http", "https"}:
            return f"only http(s) URLs are allowed (got {scheme or 'no scheme'!s})"
        if scheme == "http" and host in self.https_hosts:
            return "refusing to downgrade from https to http"
        port = _port(url)
        if port is None:
            return "URL has an invalid port"
        if port != _DEFAULT_PORTS[scheme] and (host, port) not in self.ports:
            return f"port {port} on {host!r} is not the port of the job's stored URL"
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
        """May a (GET) request go to this URL at all? Same rule as navigations."""
        if url.startswith(_BENIGN_PREFIXES) or url.startswith(("data:", "blob:")):
            return True
        return self.navigation_problem(url) is None

    def asset_request_allowed(self, url: str) -> bool:
        """A static-file GET to the job's ATS hosts / ATS CDN allowlist (no navigation, no XHR)."""
        host = _host(url)
        if not host or host not in (self.asset_hosts | self.ats_hosts):
            return False
        return urlsplit(url).scheme.lower() == "https" and _port(url) == 443

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
