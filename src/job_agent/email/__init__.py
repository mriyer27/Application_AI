"""Gmail integration (official Gmail API, OAuth 2.0, read-only scopes only).

* ``auth``         OAuth authorization, token storage (0600), refresh, local revoke
* ``gmail_client`` read-only wrapper over the Gmail API (list / get only)
* ``parse``        headers + plain-text body extraction (bodies stay in memory)
* ``classify``     deterministic rules classifier (+ optional local LLM)
* ``associate``    link messages to jobs / applications with a confidence
* ``status``       deterministic, evidence-based application status updates
* ``replies``      template-based suggested replies (stored, never sent)
* ``repository``   email_messages / email_associations persistence
* ``service``      ``sync_gmail``, ``associate_email``, ``email_sync_task``

Nothing in this package can send, draft, modify, label or delete mail.
"""
