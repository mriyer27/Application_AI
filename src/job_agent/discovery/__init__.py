"""Job discovery: polite fetching from public job-board APIs, normalization,
filtering, deduplication and persistence.

Entry points:

* ``job_agent.discovery.service.discover_jobs`` -- run discovery for configured sources
* ``job_agent.discovery.service.discover_task`` -- the ``discover`` task in ``tasks.TASKS``
* ``job_agent.discovery.commands.register`` -- CLI commands (``discover``, ``sources``)

Only public, documented job-board APIs are used (Greenhouse, Lever, Ashby). See
``docs/sources.md``.
"""
