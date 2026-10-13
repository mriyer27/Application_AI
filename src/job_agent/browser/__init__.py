"""Browser automation for application forms (Playwright, Chromium).

Layers:

* ``policy``    -- which URLs the browser may open / navigate to (host allowlist)
* ``detection`` -- pure-Python classification of page snapshots (security
  challenges, login walls, closed listings, confirmation pages)
* ``forms``     -- ``FormField`` model and the in-page inspection / fill helpers
* ``session``   -- the injectable ``BrowserSession`` / ``ApplicationPage``
  protocols and the real Playwright implementation (with its request guard)
* ``dlp``       -- recognises typed values / uploaded file content in a request
  (the guard's data-loss check before the approved submission)
* ``fakes``     -- an in-memory test double implementing the same protocols

Nothing in this package decides *what* to type or *whether* to submit; that is
``job_agent.applications``. This package never bypasses CAPTCHAs, logins or
anti-bot checks: it detects them and raises ``HumanActionRequired``.
"""
