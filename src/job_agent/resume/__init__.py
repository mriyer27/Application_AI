"""Truthful, tailored résumés and cover letters.

Every bullet and every claim-bearing sentence is built from a verified
``profile.Fact`` and keeps its fact id(s) for traceability. Optional local-LLM
rewording is accepted only when the deterministic validator
(``resume.validation.validate_claim``) finds no issue; otherwise the verbatim
fact text is used. Entry point: ``resume.service.prepare_documents``.
"""
