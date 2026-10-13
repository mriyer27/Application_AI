"""Authenticated, mobile-first web dashboard (FastAPI) for monitoring and approvals.

Entry point: ``job_agent.dashboard.app.create_app``. CLI: ``job-agent dashboard``.
The dashboard reads the database directly (``queries``) and performs actions only
through the contracts in ``actions`` -- never by running shell commands or code.
"""
