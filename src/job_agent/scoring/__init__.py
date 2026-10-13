"""Explainable job scoring: skill taxonomy, eligibility flags, rules engine, persistence and ranking.

Public entry points:

* ``engine.score_job(job, profile, config, analysis=None) -> models.ScoreResult`` (pure)
* ``eligibility.detect_eligibility_flags(job, profile) -> list[models.EligibilityFlag]``
* ``service.score_jobs(ctx, ...) -> ScoringSummary`` / ``service.score_task`` (``tasks.TASKS["score"]``)
* ``repository.save_score``, ``top_jobs``, ``job_detail``, ``set_user_relevance``, ``set_user_note``
"""
