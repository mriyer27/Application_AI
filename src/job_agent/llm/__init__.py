"""Local LLM integration (Ollama on localhost only; no cloud APIs).

Public entry points:

* ``client.OllamaClient`` / ``client.get_client`` -- HTTP client with health
  probe, bounded retries and schema-validated JSON outputs.
* ``analysis.analyze_job`` / ``analysis.make_analyzer`` -- validated job
  analysis (hallucinated skills and non-verbatim sponsorship text are dropped
  by deterministic code).
* ``generation`` -- drafting helpers (fact rewording, cover letter, email
  classification and replies, follow-ups, ordinary form answers).
* ``hardware`` / ``benchmark`` -- hardware-aware model recommendations and a
  measurement tool.

The model never has tools, never selects actions, and its output is always
validated by Pydantic plus deterministic checks before anything uses it.
"""
